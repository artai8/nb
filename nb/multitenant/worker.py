"""Lease-based multi-tenant Telegram forwarding workers."""

from __future__ import annotations

import asyncio
import logging
import os
import random
import re
import shutil
import tempfile
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

os.environ.setdefault("NB_MULTITENANT", "1")

# Anti-ban send spacing (防封): every forwarded group waits a fresh random
# 150-300 seconds before the next one, in both live and past modes.
SEND_INTERVAL_MIN_SECONDS = 150.0
SEND_INTERVAL_MAX_SECONDS = 300.0


def _random_send_interval() -> float:
    return random.uniform(SEND_INTERVAL_MIN_SECONDS, SEND_INTERVAL_MAX_SECONDS)

from telethon import TelegramClient, events, utils as telethon_utils
from telethon.errors import AuthKeyUnregisteredError, FloodWaitError, UserAlreadyParticipantError
from telethon.sessions import StringSession
from telethon.tl.functions.channels import GetFullChannelRequest, JoinChannelRequest
from telethon.tl.functions.messages import CheckChatInviteRequest, GetDiscussionMessageRequest
from telethon.tl.types import (
    Channel,
    ChatInviteAlready,
    InputMediaUploadedDocument,
    InputMediaUploadedPhoto,
    MessageMediaPhoto,
    MessageService,
)

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
    comment_destinations: list[int] = field(default_factory=list)
    source_discussion_id: Optional[int] = None
    # monotonic timestamp before which this task must not send again (live).
    next_send_at: float = 0.0

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
        if plugin_config.mark.check:
            for slot in ("intro", "midroll", "outro"):
                asset_id = getattr(task, f"ad_{slot}_asset_id", None)
                if not asset_id:
                    continue
                data = await self.db(
                    self.repo.get_ad_asset,
                    self.tenant_id,
                    asset_id,
                )
                if data:
                    ad_file = temp_dir / f"ad_{slot}.mp4"
                    ad_file.write_bytes(data)
                    setattr(plugin_config.mark, f"ad_{slot}_path", str(ad_file))
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

        if live:
            wait = runtime.next_send_at - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
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
                reply_to = await self._resolve_reply_to(
                    runtime, source_id, messages, destination.peer_id
                )
                sent = await self._send_templates(
                    task, destination.peer_id, templates, reply_to=reply_to
                )
                sent_id = _message_id([m for _, m in sent])
                # Note: message_map keys are the *source* ids; each template
                # wraps its source message (template.message.id).
                message_map = {
                    str(t.message.id): int(m.id)
                    for t, m in sent
                    if getattr(t.message, "id", None) is not None
                    and getattr(m, "id", None) is not None
                }
                await self.db(
                    self.repo.mark_delivery_sent,
                    delivery_key,
                    sent_id,
                    message_map=message_map,
                )
                if sent_id:
                    sent_count += 1
            except Exception:
                await self.db(self.repo.release_delivery, delivery_key)
                raise
        for template in templates:
            template.clear()
        if live and sent_count > 0:
            runtime.next_send_at = time.monotonic() + _random_send_interval()
        return sent_count

    async def _resolve_reply_to(
        self, runtime: TaskRuntime, source_id: int, messages: list, dest_peer_id: int
    ):
        """Map a source message's reply target to the matching destination message.

        Returns None when the group does not reply to anything, or when the
        replied-to post was never forwarded here (filtered / not yet sent) --
        the message then goes out as a top-level post rather than threading
        onto the wrong one.
        """
        first = messages[0] if messages else None
        reply_to = getattr(first, "reply_to", None) if first is not None else None
        target = getattr(reply_to, "reply_to_msg_id", None) if reply_to is not None else None
        if not target:
            return None
        return await self.db(
            self.repo.find_sent_message,
            self.tenant_id,
            runtime.task.id,
            source_id,
            int(target),
            dest_peer_id,
        )

    async def _send_templates(self, task: ForwardTask, destination: int, templates: list, *, reply_to=None):
        """Send templates; return (template, sent_message) pairs in send order.

        The pairs are the source->destination message mapping: each template
        wraps the original source message (template.message.id), so the caller
        can persist message_map for reply/edit/delete/comment lookups.
        """
        assert self.client is not None
        media_templates = [template for template in templates if getattr(template.message, "media", None)]
        # Inline buttons are only sent when the inline plugin is on (its mode
        # already transformed or removed them); otherwise they are dropped,
        # as in the original (stale buttons cause send errors).
        buttons_on = bool(getattr(task.plugins.inline, "check", False))
        sent: list = []
        if media_templates:
            captions = [template.text or "" for template in media_templates]
            for group in _chunks(media_templates):
                files = [await self._file_for_send(t) for t in group]
                group_captions = captions[:len(group)]
                captions = captions[len(group):]
                buttons = group[-1].reply_markup if buttons_on else None
                result = await self.client.send_file(
                    destination,
                    files,
                    caption=group_captions,
                    reply_to=reply_to,
                    buttons=buttons,
                )
                results = result if isinstance(result, list) else [result]
                sent.extend(zip(group, results))
                reply_to = None
        else:
            for template in templates:
                if not template.text:
                    continue
                buttons = template.reply_markup if buttons_on else None
                result = await self.client.send_message(
                    destination,
                    template.text,
                    reply_to=reply_to,
                    link_preview=False,
                    buttons=buttons,
                )
                sent.append((template, result))
                reply_to = None
        return sent

    async def _file_for_send(self, template):
        """File argument for send_file, preserving the spoiler flag.

        Unrewritten media goes by reference (Telethon reads media.spoiler
        when building the InputMedia). A rewritten file (new_file) is
        uploaded and wrapped in an InputMedia with spoiler=True when the
        template is spoiler-marked, because a bare path upload drops the flag.
        """
        media = getattr(template.message, "media", None)
        spoiler = bool(media is not None and getattr(media, "spoiler", False))
        new_file = getattr(template, "new_file", None)
        if not new_file:
            return template.message.media
        if not spoiler:
            return new_file
        uploaded = await self.client.upload_file(new_file)
        if isinstance(media, MessageMediaPhoto):
            return InputMediaUploadedPhoto(file=uploaded, spoiler=True)
        doc = getattr(media, "document", None)
        return InputMediaUploadedDocument(
            file=uploaded,
            mime_type=getattr(doc, "mime_type", None) or "application/octet-stream",
            attributes=list(getattr(doc, "attributes", []) or []),
            spoiler=True,
        )

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
        """Forward discussion-group comments to the matching destination posts.

        A message here is either a channel-post mirror (``fwd_from.channel_post``:
        record the discussion->post link, do not forward) or a comment on a post
        (``reply_to_top_id`` points at the mirror). The destination post comes
        from the delivery mapping; dest_mode "comments" replies inside the
        destination post's own discussion thread, "discussion" sends to the
        configured discussion groups. Comment failures are best-effort: logged,
        never fatal to the task.
        """
        assert self.client is not None
        routes = list(self.comment_routes.get(discussion_id, []))
        if not routes:
            return
        values = [m for m in messages if not isinstance(m, MessageService)]
        if not values:
            return
        comments: list = []
        for message in values:
            fwd = getattr(message, "fwd_from", None)
            channel_post = getattr(fwd, "channel_post", None) if fwd else None
            if channel_post:
                for runtime, _src in routes:
                    await self.db(
                        self.repo.save_comment_link,
                        self.tenant_id,
                        runtime.task.id,
                        discussion_id,
                        int(message.id),
                        int(channel_post),
                    )
                continue
            comments.append(message)
        if not comments:
            return
        for group in self._group_messages(comments):
            for runtime, source_peer_id in routes:
                if runtime.task.status not in {TaskStatus.ACTIVE, TaskStatus.RUNNING}:
                    continue
                try:
                    await self._forward_comment_group(
                        runtime, source_peer_id, discussion_id, group
                    )
                except FloodWaitError:
                    raise
                except Exception:
                    logging.exception(
                        "Comment forward failed tenant=%s task=%s",
                        self.tenant_id,
                        runtime.task.id,
                    )

    async def _forward_comment_group(
        self, runtime: TaskRuntime, source_peer_id: int, discussion_id: int, group: list
    ) -> None:
        task = runtime.task
        cfg = task.comments
        first = group[0]
        if cfg.only_media and not getattr(first, "media", None):
            return
        if not cfg.include_text_comments and not getattr(first, "media", None):
            return
        if cfg.skip_bot_comments:
            try:
                sender = await first.get_sender()
            except Exception:
                sender = None
            if sender is not None and getattr(sender, "bot", False):
                return
        reply_to = getattr(first, "reply_to", None)
        top_id = None
        if reply_to is not None:
            top_id = getattr(reply_to, "reply_to_top_id", None) or getattr(
                reply_to, "reply_to_msg_id", None
            )
        if top_id is None:
            return  # not attached to any channel post
        channel_post_id = await self.db(
            self.repo.get_comment_link,
            self.tenant_id,
            task.id,
            discussion_id,
            int(top_id),
        )
        if channel_post_id is None:
            try:
                top_msg = await self.client.get_messages(discussion_id, ids=int(top_id))
            except Exception:
                top_msg = None
            fwd = getattr(top_msg, "fwd_from", None) if top_msg else None
            channel_post_id = getattr(fwd, "channel_post", None) if fwd else None
            if channel_post_id:
                await self.db(
                    self.repo.save_comment_link,
                    self.tenant_id,
                    task.id,
                    discussion_id,
                    int(top_id),
                    int(channel_post_id),
                )
        if channel_post_id is None:
            return
        for destination in task.resolved_destinations:
            dest_post_id = await self.db(
                self.repo.find_sent_message,
                self.tenant_id,
                task.id,
                source_peer_id,
                int(channel_post_id),
                destination.peer_id,
            )
            if dest_post_id is None:
                continue  # source post was never forwarded here
            if cfg.dest_mode == "discussion":
                targets = [(peer, None) for peer in runtime.comment_destinations]
            else:
                disc = await self._get_discussion_message(
                    destination.peer_id, dest_post_id
                )
                if disc is None:
                    continue
                targets = [(disc.chat_id, disc.id)]
            for dest_chat_id, dest_top_id in targets:
                await self._send_comment(
                    runtime, source_peer_id, group, dest_chat_id, dest_top_id
                )

    async def _get_discussion_message(self, channel_id: int, msg_id: int):
        try:
            result = await self.client(
                GetDiscussionMessageRequest(peer=channel_id, msg_id=msg_id)
            )
            if result and result.messages:
                return result.messages[0]
        except Exception as exc:
            logging.warning(
                "Get discussion message failed channel=%s msg=%s: %s",
                channel_id,
                msg_id,
                exc,
            )
        return None

    async def _send_comment(
        self,
        runtime: TaskRuntime,
        source_peer_id: int,
        group: list,
        dest_chat_id: int,
        reply_to,
    ) -> None:
        task = runtime.task
        templates = (
            await runtime.pipeline.apply_group(group)
            if len(group) > 1
            else ([t] if (t := await runtime.pipeline.apply(group[0])) else [])
        )
        if not templates:
            return
        wait = runtime.next_send_at - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        ids = [int(m.id) for m in group]
        delivery_key = (
            f"{task.id}:{source_peer_id}:comment:{','.join(str(i) for i in ids)}:{dest_chat_id}"
        )
        if not await self.db(
            self.repo.claim_delivery,
            delivery_key,
            self.tenant_id,
            task.id,
            source_peer_id=source_peer_id,
            source_message_ids=ids,
            destination_peer_id=dest_chat_id,
        ):
            return
        try:
            sent = await self._send_templates(
                task, dest_chat_id, templates, reply_to=reply_to
            )
            sent_id = _message_id([m for _, m in sent])
            message_map = {
                str(t.message.id): int(m.id)
                for t, m in sent
                if getattr(t.message, "id", None) is not None
                and getattr(m, "id", None) is not None
            }
            await self.db(
                self.repo.mark_delivery_sent,
                delivery_key,
                sent_id,
                message_map=message_map,
            )
            if sent_id:
                runtime.next_send_at = time.monotonic() + _random_send_interval()
        except Exception:
            await self.db(self.repo.release_delivery, delivery_key)
            raise

    async def _handle_edit(self, event) -> None:
        """Sync a source-post edit to its forwarded copies (live tasks only).

        If the new text is exactly the delete trigger (default ".deleteMe"),
        delete every forwarded copy and the source message itself. Otherwise
        re-run the plugins and edit the text on each copy (text only).
        The trigger is checked on the raw text BEFORE plugins run.
        """
        assert self.client is not None
        message = getattr(event, "message", None)
        chat_id = getattr(event, "chat_id", None)
        if message is None or chat_id is None:
            return
        src_id = int(getattr(message, "id", 0) or 0)
        if not src_id:
            return
        for runtime in list(self.source_routes.get(chat_id, [])):
            task = runtime.task
            if task.mode != TaskMode.LIVE:
                continue
            if task.status not in {TaskStatus.ACTIVE, TaskStatus.RUNNING}:
                continue
            try:
                await self._sync_edit(runtime, chat_id, message, src_id)
            except FloodWaitError:
                raise
            except Exception:
                logging.exception(
                    "Edit sync failed tenant=%s task=%s",
                    self.tenant_id,
                    task.id,
                )

    async def _sync_edit(
        self, runtime: TaskRuntime, chat_id: int, message, src_id: int
    ) -> None:
        task = runtime.task
        trigger = task.live.delete_on_edit or ""
        raw_text = getattr(message, "text", None) or getattr(message, "raw_text", "") or ""
        if trigger and raw_text == trigger:
            for destination in task.resolved_destinations:
                dest_id = await self.db(
                    self.repo.find_sent_message,
                    self.tenant_id,
                    task.id,
                    chat_id,
                    src_id,
                    destination.peer_id,
                )
                if dest_id:
                    try:
                        await self.client.delete_messages(destination.peer_id, dest_id)
                    except Exception:
                        pass
            try:
                await message.delete()
            except Exception:
                pass
            return
        tm = await runtime.pipeline.apply(message)
        if tm is None:
            return
        try:
            new_text = tm.text or ""
            if not new_text:
                return
            for destination in task.resolved_destinations:
                dest_id = await self.db(
                    self.repo.find_sent_message,
                    self.tenant_id,
                    task.id,
                    chat_id,
                    src_id,
                    destination.peer_id,
                )
                if not dest_id:
                    continue
                try:
                    await self.client.edit_message(
                        destination.peer_id, dest_id, new_text
                    )
                except Exception as exc:
                    logging.warning(
                        "Edit sync failed task=%s dest=%s: %s",
                        task.id,
                        destination.peer_id,
                        exc,
                    )
        finally:
            tm.clear()

    async def _handle_delete(self, event) -> None:
        """Sync a source-post deletion to its forwarded copies.

        Only for live tasks with delete_sync on (default off, as in the
        original). Without the switch, a deletion in the source is ignored.
        """
        assert self.client is not None
        deleted_ids = getattr(event, "deleted_ids", None)
        if deleted_ids is None:
            single = getattr(event, "deleted_id", None)
            deleted_ids = [single] if single is not None else []
        if not deleted_ids:
            return
        chat_id = getattr(event, "chat_id", None)
        chats = [chat_id] if chat_id is not None else list(self.source_routes.keys())
        for raw_id in deleted_ids:
            try:
                src_id = int(raw_id)
            except (TypeError, ValueError):
                continue
            for cid in chats:
                for runtime in list(self.source_routes.get(cid, [])):
                    task = runtime.task
                    if task.mode != TaskMode.LIVE:
                        continue
                    if not task.live.delete_sync:
                        continue
                    if task.status not in {TaskStatus.ACTIVE, TaskStatus.RUNNING}:
                        continue
                    try:
                        await self._sync_delete(runtime, cid, src_id)
                    except FloodWaitError:
                        raise
                    except Exception:
                        logging.exception(
                            "Delete sync failed tenant=%s task=%s",
                            self.tenant_id,
                            task.id,
                        )

    async def _sync_delete(self, runtime: TaskRuntime, chat_id: int, src_id: int) -> None:
        task = runtime.task
        for destination in task.resolved_destinations:
            dest_id = await self.db(
                self.repo.find_sent_message,
                self.tenant_id,
                task.id,
                chat_id,
                src_id,
                destination.peer_id,
            )
            if not dest_id:
                continue
            try:
                await self.client.delete_messages(destination.peer_id, dest_id)
            except Exception:
                pass

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
                        job, task, runtime, source.peer_id, group, offsets,
                        delay=_random_send_interval()
                    ):
                        return True
                    group = [message]
                    group_key = key
            if group and not await self._forward_and_save(
                job, task, runtime, source.peer_id, group, offsets,
                delay=_random_send_interval()
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
        """执行定时推送：把各源自上次运行以来的新消息按时间正序推完。

        每日条数上限（daily_limit）是全任务跨源共享的预算，0 = 不限；
        首次运行没有进度时从最旧的消息开始搬（按上限封顶），而不是只取最
        新的几条——否则更早的历史会被永久跳过。每组间隔 150~300 秒随机
        （防封，和实时/历史一致）。任务保持 ACTIVE。返回 True 表示 job
        已由本方法关掉。"""
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
        remaining = int(task.schedule.daily_limit or 0)  # 0 = unlimited, shared across sources
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
            messages: list = []
            async for message in self.client.iter_messages(
                source.peer_id,
                min_id=saved if saved else 0,
                reverse=True,
                limit=remaining if remaining > 0 else None,
            ):
                if message is None:
                    continue
                mid = int(getattr(message, "id", 0) or 0)
                if mid <= saved:
                    continue
                messages.append(message)
            for group in self._group_messages(messages):
                if not await self._forward_and_save(
                    job, task, runtime, source.peer_id, group, offsets,
                    delay=_random_send_interval(),
                ):
                    return True
                if remaining > 0:
                    remaining -= len(group)
                    if remaining <= 0:
                        return False
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
