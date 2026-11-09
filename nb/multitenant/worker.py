"""Lease-based multi-tenant Telegram forwarding workers."""

from __future__ import annotations

import asyncio
import logging
import os
import random
import re
import shutil
import tempfile
import threading
import time
from collections import defaultdict, deque
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

os.environ.setdefault("NB_MULTITENANT", "1")

# Anti-ban send spacing (防封): ONE shared clock per tenant account — every
# outbound forward/comment send waits until the worker-level next_send_at,
# then arms a fresh random 120-300 seconds. A single account never sends
# two messages back-to-back, no matter which task they belong to.
SEND_INTERVAL_MIN_SECONDS = 120.0
SEND_INTERVAL_MAX_SECONDS = 300.0

# FloodWait handling: on any FloodWait the whole tenant pauses 300s, then
# retries the failed send. Never sleep longer than 300s per retry, even if
# Telegram asks for more — retry after 300s and re-check.
FLOOD_PAUSE_SECONDS = 300.0

# Per-item timeouts: the main loop waits at most 10 minutes for one forward
# item (one live group / one past-or-schedule group step); on timeout it
# moves on while the stuck work keeps running in the background. A stuck
# item that still cannot finish after 1 hour is cancelled and its error
# recorded (job failed / task ERROR).
ITEM_TIMEOUT_SECONDS = 600.0
ITEM_ABANDON_SECONDS = 3600.0

# Comment albums (grouped_id) wait 2s for siblings, as in the original.
COMMENT_GROUP_WAIT_SECONDS = 2.0

# Schedule catch-up: backfill at most the 3 most recent missed run dates.
SCHEDULE_CATCHUP_MAX_DAYS = 3
SCHEDULE_CATCHUP_CHECK_SECONDS = 600.0

# Worker log ring buffer (item 12): recent nb.* log records per tenant.
# Retention count lives in repository.LOG_BUFFER_MAX_LINES (single source).
LOG_BUFFER_FLUSH_MAX = 2000

# Tenant id of the asyncio task currently running, for the log handler to
# attribute records to the right tenant (the worker process hosts many).
_current_tenant_ctx: ContextVar[Optional[int]] = ContextVar(
    "nb_current_tenant", default=None
)


class _TenantLogHandler(logging.Handler):
    """Forward nb.* log records into the owning tenant's ring buffer.

    One handler per TenantWorker, all attached to the shared "nb" logger;
    each accepts only records emitted while its tenant's context is active,
    so tenants never see each other's logs. Never blocks: a full buffer
    drops the oldest lines (deque maxlen).
    """

    def __init__(self, tenant_id: int, buffer: deque, lock: threading.Lock):
        super().__init__(level=logging.INFO)
        self._tenant_id = tenant_id
        self._buffer = buffer
        self._lock = lock
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        if _current_tenant_ctx.get() != self._tenant_id:
            return
        try:
            message = self.format(record)
        except Exception:
            return
        entry = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": message,
        }
        with self._lock:
            self._buffer.append(entry)

# Merge-mode buffer cap (same as the original): when full, the oldest
# buffered post is flushed out instead of being dropped.
_MAX_MERGE_BUFFERS = 100


def _random_send_interval() -> float:
    return random.uniform(SEND_INTERVAL_MIN_SECONDS, SEND_INTERVAL_MAX_SECONDS)


def _dedupe_messages(messages: list) -> list:
    """Dedupe by (chat_id, id), keeping first-seen order."""
    seen = set()
    out = []
    for message in messages:
        key = (getattr(message, "chat_id", None), getattr(message, "id", None))
        if key in seen:
            continue
        seen.add(key)
        out.append(message)
    return out

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

from .bot_media import BotMediaResolver, _collect_discussion_comments, _msg_has_media
from .models import AccountStatus, ForwardTask, ResolvedEndpoint, TaskMode, TaskStatus
from .repository import LOG_BUFFER_MAX_LINES, MongoRepository
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

    def close(self) -> None:
        resolved = self.temp_dir.resolve()
        if resolved.exists():
            shutil.rmtree(resolved, ignore_errors=True)


@dataclass
class MergeBuffer:
    """One buffered channel post waiting for comment media to join it.

    Port of the original's MergeContext: the main post waits
    ``merge_wait_seconds`` (restarted by each merged comment); on flush the
    main messages + collected comment media go out as one album. Buffers are
    in-memory like the original -- a worker restart drops them.
    """

    task_id: str
    source_peer_id: int
    post_id: int
    runtime: TaskRuntime
    main_messages: list = field(default_factory=list)
    comment_media: list = field(default_factory=list)
    flush_task: Optional[asyncio.Task] = None


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
        self._bot_media = BotMediaResolver()
        self._merge_buffers: dict[tuple, MergeBuffer] = {}
        # Item 5: ONE shared send clock for the whole tenant account.
        self._next_send_at: float = 0.0
        self._send_lock = asyncio.Lock()
        # Item 3: tenant-wide FloodWait pause (monotonic deadline).
        self._flood_paused_until: float = 0.0
        # Item 2: comment-album buffers keyed by (discussion_id, grouped_id).
        self._comment_group_buffers: dict[tuple, dict] = {}
        # Item 7: items that timed out and keep running in the background.
        self._background_items: dict[str, dict] = {}
        # Item 12: log ring buffer (drained to Mongo every sync cycle).
        self._log_buffer: deque = deque(maxlen=LOG_BUFFER_FLUSH_MAX)
        self._log_buffer_lock = threading.Lock()
        self._log_handler: Optional[_TenantLogHandler] = None
        # Item 6: per-task last catch-up scan (monotonic), to avoid a Mongo
        # find_one per task on every 5s sync cycle.
        self._schedule_catchup_checked: dict[str, float] = {}
        self._stopped = asyncio.Event()
        self._consumer_task: Optional[asyncio.Task] = None

    async def db(self, func, *args, **kwargs):
        return await asyncio.to_thread(func, *args, **kwargs)

    async def _cleanup(self) -> None:
        if self._consumer_task is not None:
            self._consumer_task.cancel()
            await asyncio.gather(self._consumer_task, return_exceptions=True)
        for info in list(self._background_items.values()):
            info["task"].cancel()
        if self._background_items:
            await asyncio.gather(
                *(info["task"] for info in self._background_items.values()),
                return_exceptions=True,
            )
        self._background_items.clear()
        for buf in list(self._comment_group_buffers.values()):
            if buf.get("flush_task") is not None:
                buf["flush_task"].cancel()
        self._comment_group_buffers.clear()
        for rt in list(self.tasks.values()):
            rt.close()
        self.tasks.clear()
        if self.client is not None:
            try:
                await self.client.disconnect()
            except Exception:
                pass
            self.client = None
        await self._flush_log_buffer()
        if self._log_handler is not None:
            logging.getLogger("nb").removeHandler(self._log_handler)
            self._log_handler = None
        await self.db(self.repo.release_lease, self.tenant_id, self.settings.worker_id, self.epoch)

    async def _sync_and_reconcile(self) -> None:
        await self._flush_log_buffer()
        await self._reap_background_items()
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
        token = _current_tenant_ctx.set(self.tenant_id)
        self._log_handler = _TenantLogHandler(
            self.tenant_id, self._log_buffer, self._log_buffer_lock
        )
        logging.getLogger("nb").addHandler(self._log_handler)
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
            _current_tenant_ctx.reset(token)

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

    # -- shared send clock / flood pause / item timeout / logs -----------

    def _note_flood_wait(self, seconds: float) -> None:
        """Tenant-wide 300s pause after any FloodWait (item 3).

        Never sleeps longer than 300s per retry, even if Telegram asks for
        more — after the pause the failed send is retried and re-checked.
        """
        del seconds  # the pause is always a flat 300s by Hon's spec
        self._flood_paused_until = max(
            self._flood_paused_until, time.monotonic() + FLOOD_PAUSE_SECONDS
        )
        logging.warning(
            "tenant flood pause tenant=%s until=%.0f",
            self.tenant_id, self._flood_paused_until,
        )

    async def _wait_flood_pause(self) -> None:
        wait = self._flood_paused_until - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)

    async def _throttle_send(self) -> None:
        """Worker-level anti-ban throttle (item 5).

        One shared 120-300s clock for the whole tenant account: wait until
        the window opens, then arm the next window. Also respects the
        tenant-wide flood pause first. The _send_lock serializes the
        check+arm so two concurrent sends can never share a window.
        """
        await self._wait_flood_pause()
        async with self._send_lock:
            wait = self._next_send_at - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._next_send_at = time.monotonic() + _random_send_interval()

    async def _run_item(
        self,
        key: str,
        coro,
        *,
        on_error: Optional[Callable] = None,
        description: str = "",
    ):
        """Run one forward item with a 10-minute main-loop budget (item 7).

        Returns the coroutine's result when it finishes in time (exceptions
        propagate to the caller, as before). On timeout returns None and the
        work keeps running in the background; the reaper abandons it after
        1 hour. ``on_error`` (async callable taking the exception) records
        a late failure or an abandonment.
        """
        task = asyncio.create_task(coro)
        done, _pending = await asyncio.wait({task}, timeout=ITEM_TIMEOUT_SECONDS)
        if done:
            return task.result()
        logging.warning(
            "item %s timed out after %ss (%s), continuing in background",
            key, ITEM_TIMEOUT_SECONDS, description or key,
        )
        self._background_items[key] = {
            "task": task,
            "started_at": time.monotonic(),
            "on_error": on_error,
            "description": description or key,
        }
        task.add_done_callback(lambda t, k=key: self._background_item_done(k, t))
        return None

    def _background_item_done(self, key: str, task: asyncio.Task) -> None:
        info = self._background_items.pop(key, None)
        if task.cancelled():
            return
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            return
        if exc is None:
            return
        logging.exception(
            "background item %s failed: %s", key, exc,
        )
        on_error = (info or {}).get("on_error")
        if on_error is not None:
            asyncio.create_task(on_error(exc))

    async def _finish_job_error(self, job_id: str, exc: BaseException) -> None:
        await self.db(
            self.repo.finish_job,
            job_id,
            worker_id=self.settings.worker_id,
            lease_epoch=self.epoch,
            error=f"{type(exc).__name__}: {exc}",
        )

    async def _reap_background_items(self) -> None:
        """Cancel stuck background items older than 1h and record (item 7)."""
        now = time.monotonic()
        for key, info in list(self._background_items.items()):
            task = info["task"]
            if task.done():
                continue
            if now - info["started_at"] <= ITEM_ABANDON_SECONDS:
                continue
            logging.warning(
                "abandoning stuck item %s after %ss (%s)",
                key, ITEM_ABANDON_SECONDS, info.get("description", key),
            )
            task.cancel()
            self._background_items.pop(key, None)
            exc = TimeoutError(
                f"item {key} stuck for over 1 hour, abandoned"
            )
            on_error = info.get("on_error")
            if on_error is not None:
                await on_error(exc)

    async def _flush_log_buffer(self) -> None:
        """Drain the log ring buffer into Mongo (item 12)."""
        with self._log_buffer_lock:
            records = list(self._log_buffer)
            self._log_buffer.clear()
        if not records:
            return
        try:
            await self.db(self.repo.save_log_records, self.tenant_id, records)
        except Exception:
            logging.exception("log flush failed tenant=%s", self.tenant_id)

    async def _consume(self) -> None:
        while True:
            kind, event = await self.queue.get()
            try:
                await self._run_item(
                    f"event:{kind}:{getattr(getattr(event, 'message', None), 'id', '?')}",
                    self._dispatch_event(kind, event),
                    on_error=lambda exc, e=event: self._set_event_task_errors(e, exc),
                    description=f"{kind} event",
                )
            except Exception as exc:
                logging.exception("Tenant event failed tenant=%s kind=%s", self.tenant_id, kind)
                await self._set_event_task_errors(event, exc)
            finally:
                self.queue.task_done()

    async def _dispatch_event(self, kind: str, event) -> None:
        """Handle one queued event, retrying FloodWaits after the shared pause.

        The whole-event retry matches the old _consume behaviour, but the
        wait is the tenant-wide 300s pause (item 3), not Telegram's
        requested seconds. The 1-hour abandon bound comes from the _run_item
        wrapper that runs this coroutine (item 7).
        """
        while True:
            try:
                if kind == "new":
                    if getattr(event.message, "grouped_id", None) is None:
                        await self._handle_messages(event.chat_id, [event.message])
                elif kind == "album":
                    await self._handle_messages(event.chat_id, list(event.messages))
                elif kind == "edited":
                    await self._handle_edit(event)
                elif kind == "deleted":
                    await self._handle_delete(event)
                return
            except FloodWaitError as exc:
                logging.warning(
                    "FloodWait tenant=%s kind=%s seconds=%s, pausing %ss",
                    self.tenant_id, kind, exc.seconds, FLOOD_PAUSE_SECONDS,
                )
                self._note_flood_wait(exc.seconds or 0)
                await self._wait_flood_pause()

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
                self._drop_merge_buffers(task_id)

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

    async def _forward_messages(self, runtime: TaskRuntime, source_id: int, messages: list, *, live: bool, from_merge: bool = False) -> int:
        assert self.client is not None
        task = runtime.task
        values = [message for message in messages if not isinstance(message, MessageService)]
        if not values:
            return 0
        # Merge mode (live channel posts only): hold the post in a buffer so
        # comment media can join it, instead of sending immediately. The
        # flush re-enters here with from_merge=True and skips this gate.
        if live and not from_merge and task.comments.merge_comment_media:
            if getattr(values[0], "post", False):
                if self._buffer_merge(runtime, source_id, values):
                    return 0
        # Bot media: a source post may carry a resource bot's t.me start link;
        # talk to the bot and merge the album it returns into this group
        # before plugins run (same combined-group semantics as the original).
        # The merge flush skips this: the original's merge path never did
        # bot-media resolution either.
        if task.bot_media.enabled and not from_merge:
            try:
                bot_messages = await self._bot_media.resolve_for_group(
                    self.client, values, task.bot_media
                )
            except FloodWaitError as exc:
                self._note_flood_wait(exc.seconds or 0)
                logging.warning("bot media resolution hit FloodWait for task %s", task.id)
                bot_messages = []
            except Exception:
                logging.warning("bot media resolution failed for task %s", task.id, exc_info=True)
                bot_messages = []
            if bot_messages:
                source_keys = {(getattr(m, "chat_id", None), m.id) for m in values}
                values = values + [
                    m for m in bot_messages
                    if (getattr(m, "chat_id", None), m.id) not in source_keys
                ]
        templates = (
            await runtime.pipeline.apply_group(values)
            if len(values) > 1
            else ([t] if (t := await runtime.pipeline.apply(values[0])) else [])
        )
        if not templates:
            return 0

        # Layer A flood retry (item 3): each destination send retries after
        # the tenant-wide 300s pause, giving up only after 1h (item 7 bound).
        call_started = time.monotonic()
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
                while True:
                    # Shared 120-300s throttle (item 5): one window per send,
                    # per tenant account — live, past, schedule and merge
                    # flush all go through here.
                    await self._throttle_send()
                    try:
                        sent = await self._send_templates(
                            task, destination.peer_id, templates, reply_to=reply_to
                        )
                        break
                    except FloodWaitError as exc:
                        self._note_flood_wait(exc.seconds or 0)
                        if time.monotonic() - call_started > ITEM_ABANDON_SECONDS:
                            raise
                        await self._wait_flood_pause()
                sent_id = _message_id([m for _, m in sent])
                # Note: message_map keys are the *source* ids only -- bot-media
                # templates wrap the bot's messages, which must not pollute the
                # mapping (a bot message id could collide with a source id).
                source_idents = {id(m) for m in messages}
                message_map = {
                    str(t.message.id): int(m.id)
                    for t, m in sent
                    if id(t.message) in source_idents
                    and getattr(t.message, "id", None) is not None
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
        return sent_count

    # -- comment-media merge mode (original's MergeContext) -------------

    def _buffer_merge(self, runtime: TaskRuntime, source_id: int, values: list) -> bool:
        """Hold a live channel post for comment media. Returns True when buffered."""
        task = runtime.task
        key = (task.id, source_id, int(values[0].id))
        buf = self._merge_buffers.get(key)
        if buf is None:
            if len(self._merge_buffers) >= _MAX_MERGE_BUFFERS:
                oldest_key = next(iter(self._merge_buffers))
                asyncio.create_task(self._flush_merge_buffer(oldest_key))
            buf = MergeBuffer(
                task_id=task.id,
                source_peer_id=source_id,
                post_id=int(values[0].id),
                runtime=runtime,
                main_messages=list(values),
            )
            self._merge_buffers[key] = buf
        else:
            known = {m.id for m in buf.main_messages}
            buf.main_messages.extend(m for m in values if m.id not in known)
        self._restart_merge_timer(key, task.comments.merge_wait_seconds)
        logging.info(
            "merge buffer: task=%s post=%s main=%d waiting %ss",
            task.id, buf.post_id, len(buf.main_messages),
            task.comments.merge_wait_seconds,
        )
        return True

    def _restart_merge_timer(self, key: tuple, wait_seconds: int) -> None:
        """Start (or restart) the flush timer for a buffered post."""
        buf = self._merge_buffers.get(key)
        if buf is None:
            return
        if buf.flush_task is not None:
            buf.flush_task.cancel()
        buf.flush_task = asyncio.create_task(self._merge_flush_later(key, wait_seconds))

    async def _merge_flush_later(self, key: tuple, wait_seconds: int) -> None:
        try:
            await asyncio.sleep(wait_seconds)
        except asyncio.CancelledError:
            return
        await self._flush_merge_buffer(key)

    async def _flush_merge_buffer(self, key: tuple) -> None:
        """Send a buffered post + its collected comment media as one album."""
        buf = self._merge_buffers.pop(key, None)
        if buf is None:
            return
        runtime = self.tasks.get(buf.task_id)
        if runtime is None or runtime.task.status not in {
            TaskStatus.ACTIVE, TaskStatus.RUNNING,
        }:
            logging.info(
                "merge buffer dropped: task=%s post=%s no longer active",
                buf.task_id, buf.post_id,
            )
            return
        combined = _dedupe_messages(buf.main_messages + buf.comment_media)
        if not combined:
            return
        logging.info(
            "merge flush: task=%s post=%s main=%d comment_media=%d",
            buf.task_id, buf.post_id,
            len(buf.main_messages), len(buf.comment_media),
        )
        # The send retries inside _forward_messages with the tenant-wide
        # 300s flood pause for up to 1h (items 3+7); reaching here with a
        # FloodWait means it gave up — the buffered post is dropped.
        try:
            await self._forward_messages(
                runtime, buf.source_peer_id, combined,
                live=True, from_merge=True,
            )
        except FloodWaitError as exc:
            logging.exception(
                "merge flush abandoned after flood retries task=%s post=%s seconds=%s",
                buf.task_id, buf.post_id, exc.seconds,
            )
        except Exception:
            logging.exception(
                "merge flush failed task=%s post=%s",
                buf.task_id, buf.post_id,
            )

    def _drop_merge_buffers(self, task_id: str) -> None:
        """Cancel and drop every buffered post of a task (task removed/paused)."""
        for key in [k for k in self._merge_buffers if k[0] == task_id]:
            buf = self._merge_buffers.pop(key)
            if buf.flush_task is not None:
                buf.flush_task.cancel()

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
            # A text-only source post (e.g. a bare start link) plus bot media
            # would otherwise lose its text entirely; fold it into the first
            # caption, mirroring the original's combined_caption.
            text_only = [
                template.text
                for template in templates
                if not getattr(template.message, "media", None) and template.text
            ]
            if text_only and captions:
                captions[0] = "\n\n".join(text_only + [captions[0]]) if captions[0] else "\n\n".join(text_only)
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
        # Item 2: comments sharing a grouped_id wait ~2s for siblings (the
        # original's comment-album buffer); lone comments send immediately.
        ungrouped = [m for m in comments if getattr(m, "grouped_id", None) is None]
        grouped: dict[int, list] = {}
        for message in comments:
            gid = getattr(message, "grouped_id", None)
            if gid is not None:
                grouped.setdefault(int(gid), []).append(message)
        for message in ungrouped:
            await self._forward_comment_to_routes(discussion_id, [message])
        for gid, messages in grouped.items():
            self._buffer_comment_group(discussion_id, gid, messages)

    async def _forward_comment_to_routes(self, discussion_id: int, group: list) -> None:
        for runtime, source_peer_id in list(self.comment_routes.get(discussion_id, [])):
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

    def _buffer_comment_group(self, discussion_id: int, grouped_id: int, messages: list) -> None:
        """Hold a comment album ~2s for siblings, then flush once (item 2)."""
        key = (discussion_id, grouped_id)
        buf = self._comment_group_buffers.get(key)
        if buf is None:
            buf = {"messages": [], "flush_task": None}
            self._comment_group_buffers[key] = buf
        known = {int(m.id) for m in buf["messages"]}
        buf["messages"].extend(m for m in messages if int(m.id) not in known)
        if buf["flush_task"] is not None:
            buf["flush_task"].cancel()
        buf["flush_task"] = asyncio.create_task(self._comment_flush_later(key))

    async def _comment_flush_later(self, key: tuple) -> None:
        try:
            await asyncio.sleep(COMMENT_GROUP_WAIT_SECONDS)
        except asyncio.CancelledError:
            return
        await self._flush_comment_group(key)

    async def _flush_comment_group(self, key: tuple) -> None:
        buf = self._comment_group_buffers.pop(key, None)
        if buf is None or not buf["messages"]:
            return
        discussion_id, _gid = key
        messages = list(buf["messages"])
        try:
            await self._forward_comment_to_routes(discussion_id, messages)
        except FloodWaitError as exc:
            # _send_comment already retried with the shared pause for up to
            # 1h; this is a flood from the link/dest lookup — pause and
            # retry the group once, then drop it.
            self._note_flood_wait(exc.seconds or 0)
            await self._wait_flood_pause()
            try:
                await self._forward_comment_to_routes(discussion_id, messages)
            except Exception:
                logging.exception("comment group flush retry failed key=%s", key)
        except Exception:
            logging.exception("comment group flush failed key=%s", key)

    async def _forward_comment_group(
        self, runtime: TaskRuntime, source_peer_id: int, discussion_id: int, group: list
    ) -> None:
        task = runtime.task
        cfg = task.comments
        first = group[0]
        # Filtering (media-only, skip-bot) lives in _forward_comments_to_dest,
        # shared with past/schedule; here only the channel post is resolved.
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
        # Merge mode: media comments join the buffered album for this post;
        # anything that cannot join is discarded, exactly like the original
        # (the post is not forwarded yet while buffered, so there is no
        # destination post to reply to here).
        if cfg.merge_comment_media:
            key = (task.id, source_peer_id, int(channel_post_id))
            buf = self._merge_buffers.get(key)
            if buf is not None:
                media = [m for m in group if _msg_has_media(m)]
                if media:
                    buf.comment_media.extend(media)
                    self._restart_merge_timer(key, cfg.merge_wait_seconds)
                    logging.info(
                        "merge buffer: task=%s post=%s comment media +%d (total %d)",
                        task.id, channel_post_id, len(media), len(buf.comment_media),
                    )
            return
        await self._forward_comments_to_dest(
            runtime, source_peer_id, int(channel_post_id), group
        )

    async def _forward_comments_to_dest(
        self, runtime: TaskRuntime, source_peer_id: int, channel_post_id: int, comments: list
    ) -> None:
        """Sync one channel post's discussion comments to its destinations.

        Shared by live comment sync (_forward_comment_group) and by the
        past/schedule path (_forward_and_save). Only media comments are
        forwarded -- a media comment's own caption travels with it, but
        text-only comments are never synced, whatever
        ``include_text_comments`` says (comment sync is for resources, not
        chatter). Comments are grouped by grouped_id without waiting: live
        groups arrive pre-buffered, past/schedule comments already exist.
        At most ``recent_limit`` comments per post.
        """
        task = runtime.task
        cfg = task.comments
        limit = max(0, int(getattr(task.bot_media, "recent_limit", 80) or 80))
        values = [
            m for m in list(comments)[:limit]
            if not isinstance(m, MessageService)
            and not getattr(getattr(m, "fwd_from", None), "channel_post", None)
        ]
        if not values:
            return
        ungrouped = [m for m in values if getattr(m, "grouped_id", None) is None]
        grouped: dict[int, list] = {}
        for message in values:
            gid = getattr(message, "grouped_id", None)
            if gid is not None:
                grouped.setdefault(int(gid), []).append(message)
        groups = [[m] for m in ungrouped] + list(grouped.values())
        for group in groups:
            media = [m for m in group if _msg_has_media(m)]
            if not media:
                continue  # text-only comments are never synced
            if cfg.skip_bot_comments:
                try:
                    sender = await media[0].get_sender()
                except Exception:
                    sender = None
                if sender is not None and getattr(sender, "bot", False):
                    continue
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
                        runtime, source_peer_id, media, dest_chat_id, dest_top_id
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
        # Layer A flood retry (item 3), same as _forward_messages.
        call_started = time.monotonic()
        try:
            while True:
                # Shared 120-300s throttle (item 5).
                await self._throttle_send()
                try:
                    sent = await self._send_templates(
                        task, dest_chat_id, templates, reply_to=reply_to
                    )
                    break
                except FloodWaitError as exc:
                    self._note_flood_wait(exc.seconds or 0)
                    if time.monotonic() - call_started > ITEM_ABANDON_SECONDS:
                        raise
                    await self._wait_flood_pause()
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
            if now.strftime("%H:%M") >= task.schedule.run_time:
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
            await self._create_schedule_catchup_jobs(task, now)

    async def _create_schedule_catchup_jobs(self, task: ForwardTask, now: datetime) -> None:
        """Backfill missed schedule run dates (item 6).

        A whole day down used to lose that day's run forever. On each sync
        cycle (throttled to once per 10 minutes per task) the most recent
        missed dates — at most 3, most recent first, never before the task
        was created — get catch-up jobs. The job_key uniqueness makes this
        idempotent: a date that already has a job is skipped.
        """
        last = self._schedule_catchup_checked.get(task.id, 0.0)
        if time.monotonic() - last < SCHEDULE_CATCHUP_CHECK_SECONDS:
            return
        self._schedule_catchup_checked[task.id] = time.monotonic()
        try:
            hour, minute = (int(value) for value in task.schedule.run_time.split(":"))
        except ValueError:
            return
        today = now.date()
        created = task.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        earliest = max(
            created.astimezone(SHANGHAI).date(),
            today - timedelta(days=SCHEDULE_CATCHUP_MAX_DAYS),
        )
        for offset in range(1, SCHEDULE_CATCHUP_MAX_DAYS + 1):
            day = today - timedelta(days=offset)
            if day < earliest:
                continue
            key = f"schedule:{task.id}:{day.isoformat()}"
            if await self.db(self.repo.job_exists, key):
                continue
            scheduled_at = datetime(
                day.year, day.month, day.day, hour, minute, tzinfo=SHANGHAI
            ).astimezone(timezone.utc)
            created_id = await self.db(
                self.repo.create_job,
                self.tenant_id,
                task.id,
                kind="schedule",
                job_key=key,
                scheduled_at=scheduled_at,
                task_revision=task.revision,
            )
            if created_id:
                logging.info(
                    "schedule catch-up: task=%s missed date=%s job=%s",
                    task.id, day.isoformat(), created_id,
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

    async def _forward_and_save(
        self, job, task, runtime, source_id: int, group: list, offsets: dict
    ) -> bool:
        """发一组消息并把进度写回任务；返回 False 表示任务中途被改/被停/租约丢了，调用方应中止。

        Flood retries live inside _forward_messages (tenant-wide 300s pause,
        1h bound). The per-group delay is gone: the shared worker-level
        throttle (item 5) spaces sends. Offsets only move forward (max) so
        a late-finishing background group can never rewind progress.

        Batch 8: past/schedule groups also honour the task's comment
        switches. The discussion comments of the group's first post are
        read once (old comments already exist -- no merge wait). Merge on:
        media comments join this group as one album (one throttle slot,
        like the live merge flush). Merge off + comments enabled: after
        the post itself is sent, media comments are synced to the
        destination post's discussion thread via _forward_comments_to_dest
        (text-only comments are never synced). Offsets are always computed
        from the original group only -- comment ids live in the discussion
        group's id space and must never advance the channel offset.
        """
        comments = task.comments
        post = next(
            (m for m in group if not isinstance(m, MessageService)),
            None,
        )
        disc_comments: list = []
        if post is not None and (comments.merge_comment_media or comments.enabled):
            try:
                _disc, disc_comments, _hint = await _collect_discussion_comments(
                    self.client, source_id, int(post.id), task.bot_media
                )
            except Exception:
                logging.warning(
                    "past/schedule comment read failed task=%s post=%s",
                    task.id, post.id, exc_info=True,
                )
                disc_comments = []
        send_group = group
        if comments.merge_comment_media and disc_comments:
            cap = min(int(task.bot_media.recent_limit or 80), 100)
            media = [
                m for m in disc_comments
                if not isinstance(m, MessageService) and _msg_has_media(m)
            ][:cap]
            if media:
                send_group = _dedupe_messages(list(group) + media)
                logging.info(
                    "past/schedule merge: task=%s post=%s main=%d comment_media=%d",
                    task.id, post.id, len(group), len(media),
                )
        sent_count = await self._forward_messages(runtime, source_id, send_group, live=False)
        max_id = max(int(getattr(m, "id", 0) or 0) for m in group)
        key = str(source_id)
        offsets[key] = max(int(offsets.get(key, 0) or 0), max_id)
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
        if (
            not comments.merge_comment_media
            and comments.enabled
            and disc_comments
            and sent_count > 0
            and post is not None
        ):
            try:
                await self._forward_comments_to_dest(
                    runtime, source_id, int(post.id), disc_comments
                )
            except FloodWaitError:
                raise
            except Exception:
                logging.exception(
                    "past/schedule comment sync failed task=%s post=%s",
                    task.id, post.id,
                )
        return True

    async def _forward_group_or_background(
        self, job, task, runtime, source_id: int, group: list, offsets: dict
    ):
        """Send one job group with the 10-minute main-loop budget (item 7).

        Returns True/False from _forward_and_save when it finishes in time
        (False = stop the job). Returns None on timeout: the group keeps
        sending in the background and the job moves on to the next group.
        A late failure marks the job failed via on_error.
        """
        first_id = int(getattr(group[0], "id", 0) or 0) if group else 0
        return await self._run_item(
            f"job:{job['_id']}:{source_id}:{first_id}",
            self._forward_and_save(job, task, runtime, source_id, group, offsets),
            on_error=lambda exc, jid=job["_id"]: self._finish_job_error(jid, exc),
            description=f"job group {source_id}:{first_id}",
        )

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
                    if group and await self._forward_group_or_background(
                        job, task, runtime, source.peer_id, group, offsets
                    ) is False:
                        return True
                    group = [message]
                    group_key = key
            if group and await self._forward_group_or_background(
                job, task, runtime, source.peer_id, group, offsets
            ) is False:
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
        新的几条——否则更早的历史会被永久跳过。发送间隔由租户级共享节流
        控制（120~300 秒随机，和实时/历史一致）。任务保持 ACTIVE。返回
        True 表示 job 已由本方法关掉。"""
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
                if await self._forward_group_or_background(
                    job, task, runtime, source.peer_id, group, offsets
                ) is False:
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
