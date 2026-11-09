"""Lease-based multi-tenant Telegram forwarding workers."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import tempfile
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

os.environ.setdefault("NB_MULTITENANT", "1")

from telethon import TelegramClient, events, utils as telethon_utils
from telethon.errors import AuthKeyUnregisteredError, FloodWaitError, UserAlreadyParticipantError
from telethon.sessions import StringSession
from telethon.tl.functions.channels import GetFullChannelRequest, JoinChannelRequest
from telethon.tl.functions.messages import CheckChatInviteRequest, GetDiscussionMessageRequest
from telethon.tl.types import Channel, ChatInviteAlready, MessageService

from nb.plugins import PluginPipeline

from .models import AccountStatus, ForwardTask, ResolvedEndpoint, TaskMode, TaskStatus
from .repository import MongoRepository
from .security import SessionCipher, get_telegram_proxy
from .settings import Settings

SHANGHAI = ZoneInfo("Asia/Shanghai")


class TaskStopped(RuntimeError):
    pass


def _message_id(value) -> Optional[int]:
    if isinstance(value, list):
        value = value[0] if value else None
    return int(value.id) if value is not None and getattr(value, "id", None) else None


def _chunks(values: list, size: int = 10):
    for index in range(0, len(values), size):
        yield values[index:index + size]


@dataclass
class TaskRuntime:
    task: ForwardTask
    pipeline: PluginPipeline
    temp_dir: Path
    post_mapping: dict[tuple[int, int, int], int] = field(default_factory=dict)
    message_mapping: dict[tuple[int, int, int], int] = field(default_factory=dict)
    comment_destinations: list[int] = field(default_factory=list)
    source_discussion_id: Optional[int] = None

    def close(self) -> None:
        resolved = self.temp_dir.resolve()
        if resolved.exists():
            shutil.rmtree(resolved, ignore_errors=True)


class TenantWorker:
    def __init__(self, settings: Settings, repo: MongoRepository, tenant_id: int, epoch: int):
        self.tenant_id = int(tenant_id)
        self.settings = settings
        self.repo = repo
        self.epoch = int(epoch)
        self.client: Optional[TelegramClient] = None
        self.account: dict[str, Any] = {}
        self.tasks: dict[str, TaskRuntime] = {}
        self.source_routes: dict[int, list[TaskRuntime]] = defaultdict(list)
        self.comment_routes: dict[int, list[ tuple[TaskRuntime, int] ]] = defaultdict(list)
        self.queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue(maxsize=1000)
        self.operation_lock = asyncio.Lock()
        self._stopped = asyncio.Event()
        self._consumer_task: Optional[asyncio.Task] = None

    async def db(self, func, *args, **kwargs):
        return await asyncio.to_thread(func, *args, **kwargs)

    async def _cleanup(self) -> None:
        if self._consumer_task is not None:
            self._consumer_task.cancel()
            await asyncio.gather(self._consumer_task, return_exceptions=True)
        for rt in list(self.tasks.values()):
            rt.close()
        self.tasks.clear()
        if self.client is not None:
            try:
                await self.client.disconnect()
            except Exception:
                pass
            self.client = None
        await self.db(self.repo.release_lease, self.tenant_id, self.settings.worker_id, self.epoch)

    async def _sync_and_reconcile(self) -> None:
        account = await self.db(self.repo.get_account, self.tenant_id)
        if not account:
            self._stopped.set()
            return
        if account.get("status") == AccountStatus.DELETING.value:
            if self.client is not None:
                await self._delete_connected_account()
            else:
                await self._delete_account(account)
            self._stopped.set()
            return
        if account.get("status") != AccountStatus.ACTIVE.value:
            self._stopped.set()
            return

        if self.client is None:
            session_str = SessionCipher(self.settings.session_encryption_key).decrypt(
                account["encrypted_session"]
            )
            self.client = TelegramClient(
                StringSession(session_str),
                self.settings.api_id,
                self.settings.api_hash,
                proxy=get_telegram_proxy(),
            )
            await self.client.connect()
            if not await self.client.is_user_authorized():
                await self.db(
                    self.repo.set_account_status,
                    self.tenant_id,
                    AccountStatus.REAUTH_REQUIRED,
                    error="Telegram session expired or revoked",
                )
                self._stopped.set()
                return
            self._register_handlers()
            self._consumer_task = asyncio.create_task(self._consume())

        await self._reload_tasks()
        await self._schedule_due_jobs()
        await self._run_next_job()

    async def run(self) -> None:
        owner_task = asyncio.current_task()
        lease_task = asyncio.create_task(self._lease_renewer(owner_task))
        try:
            while not self._stopped.is_set():
                await self._sync_and_reconcile()
                await asyncio.sleep(self.settings.sync_interval_seconds)
        except asyncio.CancelledError:
            pass
        finally:
            lease_task.cancel()
            await asyncio.gather(lease_task, return_exceptions=True)
            await self._cleanup()

    async def _lease_renewer(self, owner_task: Optional[asyncio.Task]) -> None:
        while not self._stopped.is_set():
            try:
                await asyncio.sleep(max(1.0, self.settings.lease_seconds / 3))
                renewed = await self.db(
                    self.repo.renew_lease,
                    self.tenant_id,
                    self.settings.worker_id,
                    self.epoch,
                    self.settings.lease_seconds,
                )
                if renewed:
                    continue
                logging.warning("Lease lost tenant=%s", self.tenant_id)
                self._stopped.set()
                if owner_task:
                    owner_task.cancel()
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.exception("Lease renewal failed tenant=%s", self.tenant_id)
                self._stopped.set()
                if owner_task:
                    owner_task.cancel()

    async def _delete_account(self, account: dict[str, Any]) -> None:
        client = None
        try:
            session = SessionCipher(self.settings.session_encryption_key).decrypt(
                account["encrypted_session"]
            )
            client = TelegramClient(
                StringSession(session),
                self.settings.api_id,
                self.settings.api_hash,
                proxy=get_telegram_proxy(),
            )
            await client.connect()
            try:
                revoked = await client.log_out()
                if revoked is False:
                    raise RuntimeError("Telegram did not confirm authorization revocation")
            except AuthKeyUnregisteredError:
                pass
            await self.db(self.repo.hard_delete_account, self.tenant_id)
        except Exception as exc:
            await self.db(
                self.repo.set_account_delete_error,
                self.tenant_id,
                f"logout retry: {type(exc).__name__}",
            )
        finally:
            if client is not None:
                await client.disconnect()

    async def _delete_connected_account(self) -> None:
        assert self.client is not None
        try:
            try:
                revoked = await self.client.log_out()
                if revoked is False:
                    raise RuntimeError("Telegram did not confirm authorization revocation")
            except AuthKeyUnregisteredError:
                pass
            await self.db(self.repo.hard_delete_account, self.tenant_id)
        except Exception as exc:
            await self.db(
                self.repo.set_account_delete_error,
                self.tenant_id,
                f"logout retry: {type(exc).__name__}",
            )

    def _register_handlers(self) -> None:
        assert self.client is not None
        self.client.add_event_handler(
            lambda event: self._enqueue_event("new", event),
            events.NewMessage(),
        )
        self.client.add_event_handler(
            lambda event: self._enqueue_event("album", event),
            events.Album(),
        )
        self.client.add_event_handler(
            lambda event: self._enqueue_event("edited", event),
            events.MessageEdited(),
        )
        self.client.add_event_handler(
            lambda event: self._enqueue_event("deleted", event),
            events.MessageDeleted(),
        )

    async def _enqueue_event(self, kind: str, event) -> None:
        if self.queue.full():
            logging.warning("Tenant queue applying backpressure tenant=%s kind=%s", self.tenant_id, kind)
        await self.queue.put((kind, event))

    async def _consume(self) -> None:
        while True:
            kind, event = await self.queue.get()
            try:
                while True:
                    try:
                        async with self.operation_lock:
                            if kind == "new":
                                if getattr(event.message, "grouped_id", None) is None:
                                    await self._handle_messages(event.chat_id, [event.message])
                            elif kind == "album":
                                await self._handle_messages(event.chat_id, list(event.messages))
                            elif kind == "edited":
                                await self._handle_edit(event)
                            elif kind == "deleted":
                                await self._handle_delete(event)
                        break
                    except FloodWaitError as exc:
                        logging.warning("FloodWait tenant=%s seconds=%s", self.tenant_id, exc.seconds)
                        await asyncio.sleep(exc.seconds)
                    except Exception as exc:
                        logging.exception("Tenant event failed tenant=%s kind=%s", self.tenant_id, kind)
                        await self._set_event_task_errors(event, exc)
                        break
            finally:
                self.queue.task_done()

    async def _set_event_task_errors(self, event, error: Exception) -> None:
        chat_id = getattr(event, "chat_id", None)
        tasks = {
            runtime.task.id: runtime.task
            for runtime in self.source_routes.get(chat_id, [])
        }
        tasks.update(
            {
                runtime.task.id: runtime.task
                for runtime, _ in self.comment_routes.get(chat_id, [])
            }
        )
        if not tasks and chat_id is None:
            tasks = {runtime.task.id: runtime.task for runtime in self.tasks.values()}
        for runtime in self.tasks.values():
            if runtime.task.id in tasks:
                runtime.task.status = TaskStatus.ERROR
        for task_id, task in tasks.items():
            await self.db(
                self.repo.set_task_error,
                self.tenant_id,
                task_id,
                f"{type(error).__name__}: {error}",
                expected_revision=task.revision,
                lease_epoch=self.epoch,
            )

    async def _reload_tasks(self) -> None:
        await self.db(self.repo.fence_tenant_tasks, self.tenant_id, self.epoch)
        values = await self.db(self.repo.list_runtime_tasks, self.tenant_id)
        validating = [task for task in values if task.status == TaskStatus.VALIDATING]
        for task in validating:
            await self._validate_task(task)
        if validating:
            values = await self.db(self.repo.list_runtime_tasks, self.tenant_id)

        keep = {task.id for task in values if task.status in {TaskStatus.ACTIVE, TaskStatus.RUNNING}}
        for task_id in list(self.tasks):
            if task_id not in keep:
                self.tasks.pop(task_id).close()

        for task in values:
            if task.status not in {TaskStatus.ACTIVE, TaskStatus.RUNNING}:
                continue
            existing = self.tasks.get(task.id)
            if existing and existing.task.revision == task.revision:
                continue
            if existing:
                existing.close()
            try:
                self.tasks[task.id] = await self._build_task_runtime(task)
            except Exception as exc:
                self.tasks.pop(task.id, None)
                await self.db(
                    self.repo.set_task_error,
                    self.tenant_id,
                    task.id,
                    str(exc),
                    expected_revision=task.revision,
                    lease_epoch=self.epoch,
                )

        self.source_routes.clear()
        self.comment_routes.clear()
        for runtime in self.tasks.values():
            if runtime.task.mode != TaskMode.LIVE:
                continue
            for endpoint in runtime.task.resolved_sources:
                self.source_routes[endpoint.peer_id].append(runtime)
                if runtime.task.comments.enabled:
                    try:
                        if runtime.task.comments.source_mode == "discussion":
                            discussion_id = runtime.source_discussion_id
                        else:
                            discussion_id = await self._discussion_group_id(endpoint.peer_id)
                    except Exception as exc:
                        await self.db(
                            self.repo.set_task_error,
                            self.tenant_id,
                            runtime.task.id,
                            f"Comment source validation failed: {exc}",
                            expected_revision=runtime.task.revision,
                            lease_epoch=self.epoch,
                        )
                        continue
                    if discussion_id:
                        self.comment_routes[discussion_id].append((runtime, endpoint.peer_id))

    async def _build_task_runtime(self, task: ForwardTask) -> TaskRuntime:
        temp_dir = Path(tempfile.mkdtemp(prefix="nb-tenant-"))
        plugin_config = task.plugins.model_copy(deep=True)
        if plugin_config.mark.check and task.watermark_asset_id:
            data = await self.db(
                self.repo.get_watermark,
                self.tenant_id,
                task.watermark_asset_id,
            )
            if data:
                watermark = temp_dir / "watermark.png"
                watermark.write_bytes(data)
                plugin_config.mark.image = str(watermark)
        pipeline = PluginPipeline(plugin_config, download_dir=str(temp_dir))
        await pipeline.initialize()
        comment_destinations = []
        source_discussion_id = None
        if task.comments.enabled and task.comments.source_mode == "discussion" and task.comments.source_discussion_group:
            source = await self._resolve_endpoint(str(task.comments.source_discussion_group), destination=False)
            source_discussion_id = source.peer_id
        if task.comments.enabled and task.comments.dest_mode == "discussion":
            for raw in task.comments.dest_discussion_groups:
                endpoint = await self._resolve_endpoint(str(raw), destination=True)
                comment_destinations.append(endpoint.peer_id)
        return TaskRuntime(
            task=task,
            pipeline=pipeline,
            temp_dir=temp_dir,
            comment_destinations=comment_destinations,
            source_discussion_id=source_discussion_id,
        )

    async def _validate_task(self, task: ForwardTask) -> None:
        validated = None
        try:
            if not task.sources or not task.destinations:
                raise ValueError("At least one source and destination are required")
            sources = [await self._resolve_endpoint(value, destination=False) for value in task.sources]
            destinations = [await self._resolve_endpoint(value, destination=True) for value in task.destinations]
            pipeline = PluginPipeline(task.plugins.model_copy(deep=True))
            await pipeline.initialize()
            if task.pending_action == "run":
                status = TaskStatus.RUNNING if task.mode == TaskMode.PAST else TaskStatus.ACTIVE
            else:
                status = TaskStatus.PAUSED
            validated = await self.db(
                self.repo.set_task_validation_result,
                self.tenant_id,
                task.id,
                expected_revision=task.revision,
                lease_epoch=self.epoch,
                sources=[value.model_dump(mode="json") for value in sources],
                destinations=[value.model_dump(mode="json") for value in destinations],
                status=status,
            )
            if validated is None:
                return
            if task.pending_action == "run" and task.mode == TaskMode.PAST:
                await self.db(
                    self.repo.create_job,
                    self.tenant_id,
                    task.id,
                    kind="past",
                    task_revision=validated.revision,
                )
        except Exception as exc:
            await self.db(
                self.repo.set_task_error,
                self.tenant_id,
                task.id,
                str(exc),
                expected_revision=validated.revision if validated else task.revision,
                lease_epoch=self.epoch,
            )

    async def _resolve_endpoint(self, raw: str, *, destination: bool) -> ResolvedEndpoint:
        assert self.client is not None
        value: str | int = raw.strip()
        public_username = ""
        if re.fullmatch(r"-?\d+", str(value)):
            value = int(value)
        else:
            normalized = str(value)
            if normalized.startswith("@"):
                public_username = normalized[1:]
                value = public_username
            elif "t.me/" in normalized:
                parsed = urlparse(normalized if "://" in normalized else f"https://{normalized}")
                path = parsed.path.strip("/")
                if path.startswith("+") or path.startswith("joinchat/"):
                    invite_hash = path.removeprefix("+").removeprefix("joinchat/")
                    invite = await self.client(CheckChatInviteRequest(invite_hash))
                    if not isinstance(invite, ChatInviteAlready):
                        raise ValueError("Private invite links must be joined manually first")
                    value = invite.chat
                elif path.startswith("c/"):
                    parts = path.split("/")
                    value = int(f"-100{parts[1]}")
                else:
                    public_username = path.split("/", 1)[0]
                    value = public_username
        entity = await self.client.get_entity(value)
        if public_username and isinstance(entity, Channel):
            try:
                await self.client(JoinChannelRequest(entity))
            except UserAlreadyParticipantError:
                pass
        if destination:
            await self._validate_destination_permission(entity)
        else:
            await self.client.get_messages(entity, limit=1)
        return ResolvedEndpoint(
            raw=raw,
            peer_id=telethon_utils.get_peer_id(entity),
            title=getattr(entity, "title", "") or "",
            username=getattr(entity, "username", "") or "",
        )

    async def _validate_destination_permission(self, entity) -> None:
        assert self.client is not None
        permissions = await self.client.get_permissions(entity, "me")
        if getattr(entity, "broadcast", False):
            admin = getattr(permissions, "is_admin", False) or getattr(permissions, "is_creator", False)
            can_post = getattr(permissions, "post_messages", False) or getattr(permissions, "is_creator", False)
            if not admin or not can_post:
                raise ValueError(f"No post permission in target {getattr(entity, 'title', entity)}")
        banned = getattr(permissions, "banned_rights", None)
        if banned and getattr(banned, "send_messages", False):
            raise ValueError(f"Sending is forbidden in target {getattr(entity, 'title', entity)}")

    async def _handle_messages(self, chat_id: int, messages: list) -> None:
        if chat_id in self.comment_routes:
            await self._handle_comments(chat_id, messages)
        for runtime in list(self.source_routes.get(chat_id, [])):
            if runtime.task.status not in {TaskStatus.ACTIVE, TaskStatus.RUNNING}:
                continue
            try:
                await self._forward_messages(runtime, chat_id, messages, live=True)
            except FloodWaitError:
                raise
            except Exception as exc:
                await self._mark_runtime_error(runtime, exc)

    async def _mark_runtime_error(self, runtime: TaskRuntime, error: Exception) -> None:
        logging.exception("Task runtime failed tenant=%s task=%s", self.tenant_id, runtime.task.id)
        runtime.task.status = TaskStatus.ERROR
        await self.db(
            self.repo.set_task_error,
            self.tenant_id,
            runtime.task.id,
            f"{type(error).__name__}: {error}",
            expected_revision=runtime.task.revision,
            lease_epoch=self.epoch,
        )

    async def _forward_messages(self, runtime: TaskRuntime, source_id: int, messages: list, *, live: bool) -> int:
        assert self.client is not None
        task = runtime.task
        values = [message for message in messages if not isinstance(message, MessageService)]
        if not values:
            return 0
        templates = (
            await runtime.pipeline.apply_group(values)
            if len(values) > 1
            else ([t] if (t := await runtime.pipeline.apply(values[0])) else [])
        )
        if not templates:
            return 0

        sent_count = 0
        for destination in task.resolved_destinations:
            delivery_key = f"{task.id}:{source_id}:{','.join(str(m.id) for m in messages)}:{destination.peer_id}:new"
            if not await self.db(
                self.repo.claim_delivery,
                delivery_key,
                self.tenant_id,
                task.id,
                source_peer_id=source_id,
                source_message_ids=[m.id for m in messages],
                destination_peer_id=destination.peer_id,
            ):
                continue
            try:
                sent = await self._send_templates(task, destination.peer_id, templates)
                sent_id = _message_id(sent)
                await self.db(self.repo.mark_delivery_sent, delivery_key, sent_id)
                if sent_id:
                    sent_count += 1
            except Exception:
                await self.db(self.repo.release_delivery, delivery_key)
                raise
        for template in templates:
            template.clear()
        return sent_count

    async def _send_templates(self, task: ForwardTask, destination: int, templates: list, *, reply_to=None):
        assert self.client is not None
        media_templates = [template for template in templates if getattr(template.message, "media", None)]
        sent = []
        if media_templates:
            captions = [template.text or "" for template in media_templates]
            for group in _chunks(media_templates):
                files = [template.new_file or template.message.media for template in group]
                group_captions = captions[:len(group)]
                captions = captions[len(group):]
                result = await self.client.send_file(
                    destination,
                    files,
                    caption=group_captions,
                    reply_to=reply_to,
                )
                sent.extend(result if isinstance(result, list) else [result])
                reply_to = None
        else:
            for template in templates:
                if not template.text:
                    continue
                result = await self.client.send_message(
                    destination,
                    template.text,
                    reply_to=reply_to,
                    link_preview=False,
                )
                sent.append(result)
                reply_to = None
        return sent

    async def _discussion_group_id(self, channel_id: int) -> Optional[int]:
        assert self.client is not None
        try:
            result = await self.client(GetFullChannelRequest(channel_id))
            linked = getattr(result.full_chat, "linked_chat_id", None)
            if linked:
                entity = await self.client.get_entity(linked)
                return telethon_utils.get_peer_id(entity)
        except Exception:
            return None
        return None

    async def _handle_comments(self, discussion_id: int, messages: list) -> None:
        return None

    async def _handle_edit(self, event) -> None:
        return None

    async def _handle_delete(self, event) -> None:
        return None

    async def _schedule_due_jobs(self) -> None:
        now = datetime.now(SHANGHAI)
        for runtime in self.tasks.values():
            task = runtime.task
            if task.mode != TaskMode.SCHEDULE or task.status != TaskStatus.ACTIVE:
                continue
            if now.strftime("%H:%M") < task.schedule.run_time:
                continue
            key = f"schedule:{task.id}:{now.date().isoformat()}"
            hour, minute = (int(value) for value in task.schedule.run_time.split(":"))
            scheduled_at = now.replace(hour=hour, minute=minute, second=0, microsecond=0).astimezone(timezone.utc)
            await self.db(
                self.repo.create_job,
                self.tenant_id,
                task.id,
                kind="schedule",
                job_key=key,
                scheduled_at=scheduled_at,
                task_revision=task.revision,
                replace_cancelled=True,
            )

    async def _run_next_job(self) -> None:
        """认领并执行一个排队的 past/schedule 任务（每次同步循环最多一个）。"""
        if self.client is None:
            return
        job = await self.db(
            self.repo.claim_job, self.tenant_id, self.settings.worker_id, self.epoch
        )
        if not job:
            return
        kind = str(job.get("kind") or "")
        closed = False
        try:
            if kind == "past":
                closed = await self._execute_past_job(job)
            elif kind == "schedule":
                closed = await self._execute_schedule_job(job)
            else:
                await self.db(
                    self.repo.finish_job,
                    job["_id"],
                    worker_id=self.settings.worker_id,
                    lease_epoch=self.epoch,
                    error=f"unknown job kind: {kind}",
                )
                return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logging.exception(
                "Job failed tenant=%s job=%s kind=%s", self.tenant_id, job.get("_id"), kind
            )
            await self.db(
                self.repo.finish_job,
                job["_id"],
                worker_id=self.settings.worker_id,
                lease_epoch=self.epoch,
                error=f"{type(exc).__name__}: {exc}",
            )
            return
        if not closed:
            await self.db(
                self.repo.finish_job,
                job["_id"],
                worker_id=self.settings.worker_id,
                lease_epoch=self.epoch,
            )

    @staticmethod
    def _group_messages(messages: list) -> list[list]:
        """把按时间正序的消息按 grouped_id 分组（相册一组，没分组的一条一组）。"""
        groups: list[list] = []
        current: list = []
        current_key = None
        for message in messages:
            key = getattr(message, "grouped_id", None)
            if key is not None and current and current_key == key:
                current.append(message)
            else:
                if current:
                    groups.append(current)
                current = [message]
                current_key = key
        if current:
            groups.append(current)
        return groups

    async def _forward_with_flood_retry(
        self, runtime, source_id: int, messages: list, *, live: bool = False
    ) -> int:
        """发一组消息；撞上 FloodWait 就等一会儿重试（最多 2 次，单次等待超过 300 秒直接放弃）。"""
        for attempt in range(3):
            try:
                return await self._forward_messages(runtime, source_id, messages, live=live)
            except FloodWaitError as exc:
                wait = int(exc.seconds or 0)
                if attempt >= 2 or wait > 300:
                    raise
                logging.warning(
                    "FloodWait tenant=%s seconds=%s retry=%s", self.tenant_id, wait, attempt + 1
                )
                await asyncio.sleep(min(wait, 300))
        return 0

    async def _forward_and_save(
        self, job, task, runtime, source_id: int, group: list, offsets: dict, *, delay: float
    ) -> bool:
        """发一组消息并把进度写回任务；返回 False 表示任务中途被改/被停/租约丢了，调用方应中止。"""
        await self._forward_with_flood_retry(runtime, source_id, group, live=False)
        max_id = max(int(getattr(m, "id", 0) or 0) for m in group)
        offsets[str(source_id)] = max_id
        ok = await self.db(
            self.repo.update_task_offsets,
            self.tenant_id,
            task.id,
            dict(offsets),
            expected_revision=task.revision,
            lease_epoch=self.epoch,
        )
        if not ok:
            await self.db(
                self.repo.cancel_job,
                job["_id"],
                worker_id=self.settings.worker_id,
                lease_epoch=self.epoch,
            )
            return False
        if delay:
            await asyncio.sleep(delay)
        return True

    async def _execute_past_job(self, job) -> bool:
        """执行历史搬运：按 [offset, end] 的消息 ID 区间从旧到新逐组转发，跑完任务变 COMPLETED。
        进度每组写回一次，中途断掉（机器 8 小时上限）下次重跑从断点继续。
        返回 True 表示 job 已由本方法关掉（cancel/finish），调用方不用再 finish。"""
        assert self.client is not None
        task = await self.db(self.repo.get_task, self.tenant_id, job["task_id"])
        if task is None or task.status != TaskStatus.RUNNING:
            await self.db(
                self.repo.cancel_job,
                job["_id"],
                worker_id=self.settings.worker_id,
                lease_epoch=self.epoch,
            )
            return True
        runtime = self.tasks.get(task.id)
        if runtime is None:
            await self.db(
                self.repo.finish_job,
                job["_id"],
                worker_id=self.settings.worker_id,
                lease_epoch=self.epoch,
                error="task runtime unavailable",
            )
            return True
        offsets = dict(task.offsets or {})
        delay = max(0, int(task.past.delay or 0))
        user_offset = int(task.past.offset or 0)
        end = task.past.end
        for source in task.resolved_sources:
            fresh = await self.db(self.repo.get_task, self.tenant_id, task.id)
            if fresh is None or fresh.status != TaskStatus.RUNNING:
                await self.db(
                    self.repo.cancel_job,
                    job["_id"],
                    worker_id=self.settings.worker_id,
                    lease_epoch=self.epoch,
                )
                return True
            saved = int(offsets.get(str(source.peer_id), 0) or 0)
            start = max(user_offset, saved + 1) if saved else user_offset
            fetch_min = start - 1 if start > 0 else 0
            fetch_max = (int(end) + 1) if end is not None else 0
            group: list = []
            group_key = None
            async for message in self.client.iter_messages(
                source.peer_id, min_id=fetch_min, max_id=fetch_max, reverse=True
            ):
                if message is None:
                    continue
                mid = int(getattr(message, "id", 0) or 0)
                if mid < start or (end is not None and mid > int(end)):
                    continue
                key = getattr(message, "grouped_id", None)
                if key is not None and group and group_key == key:
                    group.append(message)
                else:
                    if group and not await self._forward_and_save(
                        job, task, runtime, source.peer_id, group, offsets, delay=delay
                    ):
                        return True
                    group = [message]
                    group_key = key
            if group and not await self._forward_and_save(
                job, task, runtime, source.peer_id, group, offsets, delay=delay
            ):
                return True
        await self.db(
            self.repo.set_task_status,
            self.tenant_id,
            task.id,
            TaskStatus.COMPLETED,
            expected_revision=task.revision,
            lease_epoch=self.epoch,
        )
        return False

    async def _execute_schedule_job(self, job) -> bool:
        """执行定时推送：把各源自上次运行以来的新消息按时间正序推完（每日条数上限封顶）。
        首次运行没有进度时，只推最近的 N 条（上限>0 用上限，否则 20 条兜底），避免把整个频道历史搬空。
        任务保持 ACTIVE。返回 True 表示 job 已由本方法关掉。"""
        assert self.client is not None
        task = await self.db(self.repo.get_task, self.tenant_id, job["task_id"])
        if task is None or task.status != TaskStatus.ACTIVE:
            await self.db(
                self.repo.cancel_job,
                job["_id"],
                worker_id=self.settings.worker_id,
                lease_epoch=self.epoch,
            )
            return True
        runtime = self.tasks.get(task.id)
        if runtime is None:
            await self.db(
                self.repo.finish_job,
                job["_id"],
                worker_id=self.settings.worker_id,
                lease_epoch=self.epoch,
                error="task runtime unavailable",
            )
            return True
        offsets = dict(task.offsets or {})
        limit = int(task.schedule.daily_limit or 0)
        for source in task.resolved_sources:
            fresh = await self.db(self.repo.get_task, self.tenant_id, task.id)
            if fresh is None or fresh.status != TaskStatus.ACTIVE:
                await self.db(
                    self.repo.cancel_job,
                    job["_id"],
                    worker_id=self.settings.worker_id,
                    lease_epoch=self.epoch,
                )
                return True
            saved = int(offsets.get(str(source.peer_id), 0) or 0)
            if saved:
                messages: list = []
                async for message in self.client.iter_messages(
                    source.peer_id, min_id=saved, reverse=True,
                    limit=limit if limit > 0 else 500,
                ):
                    if message is None:
                        continue
                    mid = int(getattr(message, "id", 0) or 0)
                    if mid <= saved:
                        continue
                    messages.append(message)
            else:
                take = limit if limit > 0 else 20
                newest = await self.client.get_messages(source.peer_id, limit=take)
                messages = [m for m in reversed(newest or []) if m is not None]
            for group in self._group_messages(messages):
                if not await self._forward_and_save(
                    job, task, runtime, source.peer_id, group, offsets, delay=1
                ):
                    return True
        return False


TenantRuntime = TenantWorker


class Worker:
    def __init__(self, settings: Settings, repository: MongoRepository) -> None:
        self.settings = settings
        self.repo = repository
        self.runtimes: dict[int, asyncio.Task] = {}

    async def run(self) -> None:
        await asyncio.to_thread(self.repo.ensure_indexes)
        while True:
            for tenant_id, task in list(self.runtimes.items()):
                if task.done():
                    await asyncio.gather(task, return_exceptions=True)
                    self.runtimes.pop(tenant_id, None)
            capacity = self.settings.worker_capacity - len(self.runtimes)
            if capacity > 0:
                candidates = await asyncio.to_thread(self.repo.claimable_tenant_ids)
                for tenant_id in candidates:
                    if capacity <= 0:
                        break
                    if tenant_id in self.runtimes:
                        continue
                    lease = await asyncio.to_thread(
                        self.repo.claim_lease,
                        tenant_id,
                        self.settings.worker_id,
                        self.settings.lease_seconds,
                    )
                    if lease is None:
                        continue
                    runtime = TenantRuntime(self.settings, self.repo, tenant_id, int(lease["epoch"]))
                    self.runtimes[tenant_id] = asyncio.create_task(runtime.run())
                    capacity -= 1
            await asyncio.sleep(self.settings.poll_seconds)


async def async_main() -> None:
    settings = Settings.for_worker()
    repository = MongoRepository(settings.mongo_uri, settings.mongo_db)
    await Worker(settings, repository).run()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
