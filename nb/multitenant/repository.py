"""MongoDB persistence for the multi-tenant control plane."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import gridfs
from bson import ObjectId
from pymongo import ASCENDING, MongoClient, ReturnDocument
from pymongo.collection import Collection
from pymongo.errors import DuplicateKeyError

from .models import (
    AccountStatus,
    ForwardTask,
    ForwardTaskInput,
    TaskMode,
    TaskStatus,
)
from .security import expiry, hash_password, new_token, token_hash, verify_password


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RevisionConflict(RuntimeError):
    pass


DEFAULT_WEB_PASSWORD = "admin123"


class MongoRepository:
    def __init__(self, uri: str, database: str, *, client=None) -> None:
        self.client = client or MongoClient(uri, tz_aware=True)
        self.db = self.client[database]
        self.tenants: Collection = self.db.tenants
        self.accounts: Collection = self.db.telegram_accounts
        self.tasks: Collection = self.db.forward_tasks
        self.jobs: Collection = self.db.jobs
        self.leases: Collection = self.db.worker_leases
        self.sessions: Collection = self.db.web_sessions
        self.deliveries: Collection = self.db.delivery_records
        self.audit_events: Collection = self.db.audit_events
        self.rate_limits: Collection = self.db.auth_rate_limits
        self.fs = gridfs.GridFS(self.db, collection="watermark_assets")

    def ensure_indexes(self) -> None:
        self.accounts.create_index([("tenant_id", ASCENDING)], unique=True)
        self.accounts.create_index([("status", ASCENDING)])
        self.tasks.create_index([("tenant_id", ASCENDING), ("updated_at", ASCENDING)])
        self.tasks.create_index([("tenant_id", ASCENDING), ("status", ASCENDING)])
        self.jobs.create_index([("job_key", ASCENDING)], unique=True)
        self.jobs.create_index(
            [("task_id", ASCENDING), ("scheduled_at", ASCENDING)],
            unique=True,
            partialFilterExpression={"scheduled_at": {"$type": "date"}},
        )
        self.jobs.create_index([("tenant_id", ASCENDING), ("status", ASCENDING)])
        self.leases.create_index([("expires_at", ASCENDING)])
        self.sessions.create_index([("token_hash", ASCENDING)], unique=True)
        # 仅当 expires_at 为 date 类型时触发 TTL；永久有效链接不设置 expires_at 或设为远期
        self.sessions.create_index([("expires_at", ASCENDING)], expireAfterSeconds=0)
        self.deliveries.create_index([("delivery_key", ASCENDING)], unique=True)
        self.deliveries.create_index([("expires_at", ASCENDING)], expireAfterSeconds=0)
        self.audit_events.create_index([("tenant_id", ASCENDING), ("created_at", -1)])
        self.rate_limits.create_index([("expires_at", ASCENDING)], expireAfterSeconds=0)

    def ping(self) -> None:
        self.client.admin.command("ping")

    def upsert_tenant(
        self,
        tenant_id: int,
        *,
        username: str = "",
        display_name: str = "",
    ) -> None:
        now = utcnow()
        self.tenants.update_one(
            {"_id": tenant_id},
            {
                "$set": {
                    "username": username,
                    "display_name": display_name,
                    "updated_at": now,
                },
                "$setOnInsert": {"created_at": now, "status": "active"},
            },
            upsert=True,
        )

    def ensure_web_login(self, tenant_id: int, username: str = "") -> Optional[str]:
        """绑定 Telegram 账号时自动配一个网页登录账号。
        用户名优先用 Telegram 用户名，密码默认 admin123，并标记 must_change_password。
        该租户已有网页账号时不动；用户名被别的租户占了就用 tg<tenant_id> 兜底。
        返回最终的网页用户名，实在配不出时返回 None。"""
        doc = self.tenants.find_one({"_id": tenant_id}, {"web_username": 1})
        if doc and doc.get("web_username"):
            return str(doc["web_username"])
        base = (username or "").strip().lstrip("@").lower()
        candidates = [c for c in [base, f"tg{tenant_id}"] if c]
        chosen: Optional[str] = None
        for cand in candidates:
            other = self.tenants.find_one({"web_username": cand})
            if other is None or int(other["_id"]) == int(tenant_id):
                chosen = cand
                break
        if chosen is None:
            return None
        self.tenants.update_one(
            {"_id": tenant_id},
            {
                "$set": {
                    "web_username": chosen,
                    "password_hash": hash_password(DEFAULT_WEB_PASSWORD),
                    "must_change_password": True,
                    "updated_at": utcnow(),
                }
            },
        )
        self.audit(tenant_id, "tenant.web_login_created", username=chosen)
        return chosen

    def change_web_password(self, tenant_id: int, old_password: str, new_password: str) -> bool:
        """租户自助改网页密码：验旧密码，通过后更新哈希并清除 must_change_password。"""
        doc = self.tenants.find_one({"_id": tenant_id})
        if not doc or not doc.get("password_hash"):
            return False
        if not verify_password(old_password, str(doc["password_hash"])):
            return False
        self.tenants.update_one(
            {"_id": tenant_id},
            {
                "$set": {
                    "password_hash": hash_password(new_password),
                    "must_change_password": False,
                    "updated_at": utcnow(),
                }
            },
        )
        self.audit(tenant_id, "tenant.web_password_changed")
        return True

    def save_account(
        self,
        tenant_id: int,
        encrypted_session: str,
        *,
        username: str = "",
        display_name: str = "",
    ) -> None:
        now = utcnow()
        self.upsert_tenant(tenant_id, username=username, display_name=display_name)
        self.accounts.update_one(
            {"tenant_id": tenant_id},
            {
                "$set": {
                    "encrypted_session": encrypted_session,
                    "username": username,
                    "display_name": display_name,
                    "status": AccountStatus.ACTIVE.value,
                    "last_error": "",
                    "updated_at": now,
                },
                "$setOnInsert": {"created_at": now},
                "$inc": {"session_revision": 1},
            },
            upsert=True,
        )
        # 绑定 Telegram 账号时自动配一个网页登录账号（用户名用 Telegram 用户名），
        # 密码默认 admin123，并标记 must_change_password，提醒用户尽快改密。
        self.ensure_web_login(tenant_id, username)
        self.audit(tenant_id, "account.bound")

    def get_account(self, tenant_id: int) -> Optional[Dict[str, Any]]:
        return self.accounts.find_one({"tenant_id": tenant_id}, {"_id": 0})

    def set_account_status(
        self,
        tenant_id: int,
        status: AccountStatus | str,
        *,
        error: str = "",
    ) -> None:
        value = status.value if isinstance(status, AccountStatus) else status
        result = self.accounts.update_one(
            {"tenant_id": tenant_id},
            {"$set": {"status": value, "last_error": error, "updated_at": utcnow()}},
        )
        if result.matched_count and error:
            self.audit(tenant_id, "account.error")

    def request_account_delete(self, tenant_id: int) -> bool:
        result = self.accounts.update_one(
            {"tenant_id": tenant_id},
            {
                "$set": {
                    "status": AccountStatus.DELETING.value,
                    "last_error": "",
                    "updated_at": utcnow(),
                },
                "$unset": {"delete_retry_at": ""},
            },
        )
        if not result.matched_count:
            return False
        if result.modified_count:
            self.tasks.update_many(
                {"tenant_id": tenant_id},
                {
                    "$set": {
                        "status": TaskStatus.PAUSED.value,
                        "pending_action": None,
                        "updated_at": utcnow(),
                    },
                    "$inc": {"revision": 1},
                },
            )
            self.audit(tenant_id, "account.delete_requested")
        return True

    def set_account_delete_error(
        self,
        tenant_id: int,
        error: str,
        *,
        retry_seconds: int = 300,
    ) -> None:
        now = utcnow()
        result = self.accounts.update_one(
            {"tenant_id": tenant_id},
            {
                "$set": {
                    "status": AccountStatus.DELETING.value,
                    "last_error": error[:500],
                    "delete_retry_at": now + timedelta(seconds=retry_seconds),
                    "updated_at": now,
                }
            },
        )
        if result.matched_count:
            self.audit(tenant_id, "account.error")

    def hard_delete_account(self, tenant_id: int) -> None:
        for asset in self.db.watermark_assets.files.find(
            {"metadata.tenant_id": tenant_id}, {"_id": 1}
        ):
            self.fs.delete(asset["_id"])
        self.tasks.delete_many({"tenant_id": tenant_id})
        self.jobs.delete_many({"tenant_id": tenant_id})
        self.deliveries.delete_many({"tenant_id": tenant_id})
        self.sessions.delete_many({"tenant_id": tenant_id})
        self.rate_limits.delete_many({"tenant_id": tenant_id})
        self.leases.delete_one({"_id": tenant_id})
        self.accounts.delete_one({"tenant_id": tenant_id})
        self.tenants.delete_one({"_id": tenant_id})
        self.audit(tenant_id, "account.deleted")

    def create_session(
        self,
        tenant_id: int,
        *,
        kind: str,
        expires_at: Optional[datetime] = None,
    ) -> tuple[str, Optional[datetime]]:
        raw = new_token()
        doc: dict[str, Any] = {
            "token_hash": token_hash(raw),
            "tenant_id": tenant_id,
            "kind": kind,
            "created_at": utcnow(),
        }
        if expires_at is not None:
            doc["expires_at"] = expires_at
        self.sessions.insert_one(doc)
        return raw, expires_at

    def create_magic_link(self, tenant_id: int, *, ttl_minutes: int = 0) -> str:
        """
        生成 Web 登录链接 Token。
        默认 ttl_minutes=0 表示取消 5 分钟时间限制（永久有效，直到用户主动重置或登出）。
        """
        exp = expiry(minutes=ttl_minutes) if ttl_minutes > 0 else None
        raw, _ = self.create_session(tenant_id, kind="magic", expires_at=exp)
        return raw

    def consume_magic_link(
        self,
        token: str,
        *,
        session_hours: int = 87600,
        reusable: bool = True,
    ) -> Optional[tuple[str, Optional[datetime]]]:
        """
        验证并消费 Web 登录链接。
        - 已取消 5 分钟强制过期：兼容无 expires_at 字段（永久有效）或未到期的记录。
        - 默认 reusable=True：避免 Telegram 链接预览爬虫或手机浏览器预加载导致链接瞬间失效，支持多端重复打开。
        """
        now = utcnow()
        query: dict[str, Any] = {
            "token_hash": token_hash(token),
            "kind": "magic",
            "$or": [
                {"expires_at": {"$exists": False}},
                {"expires_at": None},
                {"expires_at": {"$gt": now}},
            ],
        }
        if reusable:
            doc = self.sessions.find_one(query)
        else:
            doc = self.sessions.find_one_and_delete(query)
        if doc is None:
            return None
        session_exp = expiry(hours=session_hours) if session_hours > 0 else None
        return self.create_session(
            int(doc["tenant_id"]),
            kind="browser",
            expires_at=session_exp,
        )

    def resolve_session(self, token: str) -> Optional[int]:
        if not token:
            return None
        now = utcnow()
        doc = self.sessions.find_one(
            {
                "token_hash": token_hash(token),
                "kind": {"$in": ["browser", "miniapp"]},
                "$or": [
                    {"expires_at": {"$exists": False}},
                    {"expires_at": None},
                    {"expires_at": {"$gt": now}},
                ],
            },
            {"tenant_id": 1},
        )
        return int(doc["tenant_id"]) if doc else None

    def revoke_session(self, token: str) -> None:
        if token:
            self.sessions.delete_one({"token_hash": token_hash(token)})

    def consume_rate_limit(
        self,
        scope: str,
        key: str,
        *,
        limit: int,
        window_seconds: int,
        tenant_id: Optional[int] = None,
    ) -> bool:
        now = utcnow()
        bucket = int(now.timestamp()) // window_seconds
        rate_key = f"{scope}:{key}:{bucket}"
        update: dict[str, Any] = {
            "$inc": {"count": 1},
            "$setOnInsert": {
                "scope": scope,
                "key": key,
                "expires_at": now + timedelta(seconds=window_seconds * 2),
                "created_at": now,
            },
        }
        if tenant_id is not None:
            update["$setOnInsert"]["tenant_id"] = tenant_id
        doc = self.rate_limits.find_one_and_update(
            {"_id": rate_key},
            update,
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        return int(doc["count"]) <= limit

    def create_task(self, tenant_id: int, value: ForwardTaskInput) -> ForwardTask:
        now = utcnow()
        tenant_doc = self.tenants.find_one({"_id": tenant_id}) or {}
        if tenant_doc.get("status") == "disabled":
            raise PermissionError("当前租户已被停用")
        exp = tenant_doc.get("expire_at")
        if isinstance(exp, datetime) and exp < now:
            raise PermissionError("当前租户有效期已过，请联系管理员续期")
        max_tasks = int(tenant_doc.get("max_tasks", 10))
        if max_tasks > 0 and not tenant_doc.get("is_admin", False):
            cur_count = self.tasks.count_documents({"tenant_id": tenant_id})
            if cur_count >= max_tasks:
                raise PermissionError(f"已达到当前租户最大任务配额上限 ({max_tasks} 个任务)")
        if not tenant_doc.get("allow_heavy_plugins", True) and not tenant_doc.get("is_admin", False):
            if value.plugins.mark.check or value.plugins.ocr.check:
                raise PermissionError("当前租户套餐未开启高算力水印与 OCR 插件权限")
        task_id = str(uuid.uuid4())
        doc = value.model_dump(mode="json")
        doc.update(
            {
                "_id": task_id,
                "tenant_id": tenant_id,
                "status": TaskStatus.DRAFT.value,
                "revision": 1,
                "runtime_epoch": 0,
                "pending_action": None,
                "resolved_sources": [],
                "resolved_destinations": [],
                "offsets": {},
                "last_error": "",
                "created_at": now,
                "updated_at": now,
            }
        )
        self.tasks.insert_one(doc)
        self.audit(tenant_id, "task.created", task_id=task_id)
        return self._task_model(doc)

    def list_tasks(self, tenant_id: int) -> list[ForwardTask]:
        return [
            self._task_model(doc)
            for doc in self.tasks.find({"tenant_id": tenant_id}).sort("created_at", 1)
        ]

    def get_task(self, tenant_id: int, task_id: str) -> Optional[ForwardTask]:
        doc = self.tasks.find_one({"_id": task_id, "tenant_id": tenant_id})
        return self._task_model(doc) if doc else None

    def update_task(
        self,
        tenant_id: int,
        task_id: str,
        expected_revision: int,
        value: ForwardTaskInput,
    ) -> ForwardTask:
        current = self.tasks.find_one(
            {"_id": task_id, "tenant_id": tenant_id, "revision": expected_revision},
            {"status": 1},
        )
        if current is None:
            if self.tasks.count_documents({"_id": task_id, "tenant_id": tenant_id}) == 0:
                raise KeyError(task_id)
            raise RevisionConflict(task_id)
        resume = current.get("status") == TaskStatus.ACTIVE.value and value.mode in {
            TaskMode.LIVE,
            TaskMode.SCHEDULE,
        }
        update = value.model_dump(mode="json")
        update.update(
            {
                "status": TaskStatus.VALIDATING.value if resume else TaskStatus.DRAFT.value,
                "pending_action": "run" if resume else None,
                "resolved_sources": [],
                "resolved_destinations": [],
                "last_error": "",
                "updated_at": utcnow(),
            }
        )
        doc = self.tasks.find_one_and_update(
            {
                "_id": task_id,
                "tenant_id": tenant_id,
                "revision": expected_revision,
            },
            {"$set": update, "$inc": {"revision": 1}},
            return_document=ReturnDocument.AFTER,
        )
        if doc is None:
            raise RevisionConflict(task_id)
        self.audit(tenant_id, "task.updated", task_id=task_id)
        return self._task_model(doc)

    def set_task_action(self, tenant_id: int, task_id: str, action: str) -> ForwardTask:
        if action not in {"validate", "run", "pause"}:
            raise ValueError(action)
        if action == "pause":
            update = {
                "status": TaskStatus.PAUSED.value,
                "pending_action": None,
                "updated_at": utcnow(),
            }
        else:
            update = {
                "status": TaskStatus.VALIDATING.value,
                "pending_action": action,
                "last_error": "",
                "updated_at": utcnow(),
            }
        doc = self.tasks.find_one_and_update(
            {"_id": task_id, "tenant_id": tenant_id},
            {"$set": update, "$inc": {"revision": 1}},
            return_document=ReturnDocument.AFTER,
        )
        if doc is None:
            raise KeyError(task_id)
        self.audit(tenant_id, f"task.{action}", task_id=task_id)
        return self._task_model(doc)

    def set_task_validation_result(
        self,
        tenant_id: int,
        task_id: str,
        *,
        expected_revision: int,
        lease_epoch: Optional[int] = None,
        sources: list[dict[str, Any]],
        destinations: list[dict[str, Any]],
        status: TaskStatus,
        error: str = "",
    ) -> Optional[ForwardTask]:
        query: dict[str, Any] = {
            "_id": task_id,
            "tenant_id": tenant_id,
            "revision": expected_revision,
            "status": TaskStatus.VALIDATING.value,
        }
        if lease_epoch is not None:
            query["runtime_epoch"] = lease_epoch
        doc = self.tasks.find_one_and_update(
            query,
            {
                "$set": {
                    "resolved_sources": sources,
                    "resolved_destinations": destinations,
                    "status": status.value,
                    "pending_action": None,
                    "last_error": error,
                    "updated_at": utcnow(),
                },
                "$inc": {"revision": 1},
            },
            return_document=ReturnDocument.AFTER,
        )
        return self._task_model(doc) if doc else None

    def set_task_error(
        self,
        tenant_id: int,
        task_id: str,
        error: str,
        *,
        expected_revision: Optional[int] = None,
        lease_epoch: Optional[int] = None,
    ) -> bool:
        query: dict[str, Any] = {"_id": task_id, "tenant_id": tenant_id}
        if expected_revision is not None:
            query["revision"] = expected_revision
        if lease_epoch is not None:
            query["runtime_epoch"] = lease_epoch
        result = self.tasks.update_one(
            query,
            {
                "$set": {
                    "status": TaskStatus.ERROR.value,
                    "last_error": error[:2000],
                    "pending_action": None,
                    "updated_at": utcnow(),
                },
                "$inc": {"revision": 1},
            },
        )
        if result.matched_count:
            self.audit(tenant_id, "task.error", task_id=task_id)
        return result.matched_count == 1

    def set_task_status(
        self,
        tenant_id: int,
        task_id: str,
        status: TaskStatus,
        *,
        error: str = "",
        expected_revision: Optional[int] = None,
        lease_epoch: Optional[int] = None,
    ) -> Optional[ForwardTask]:
        query: dict[str, Any] = {"_id": task_id, "tenant_id": tenant_id}
        if expected_revision is not None:
            query["revision"] = expected_revision
        if lease_epoch is not None:
            query["runtime_epoch"] = lease_epoch
        doc = self.tasks.find_one_and_update(
            query,
            {
                "$set": {
                    "status": status.value,
                    "last_error": error[:2000],
                    "updated_at": utcnow(),
                },
                "$inc": {"revision": 1},
            },
            return_document=ReturnDocument.AFTER,
        )
        if doc is not None and status == TaskStatus.COMPLETED:
            self.audit(tenant_id, "task.completed", task_id=task_id)
        return self._task_model(doc) if doc else None

    def update_task_offsets(
        self,
        tenant_id: int,
        task_id: str,
        offsets: dict[str, int],
        *,
        expected_revision: int,
        lease_epoch: Optional[int] = None,
    ) -> bool:
        query: dict[str, Any] = {
            "_id": task_id,
            "tenant_id": tenant_id,
            "revision": expected_revision,
            "status": {"$in": [TaskStatus.ACTIVE.value, TaskStatus.RUNNING.value]},
        }
        if lease_epoch is not None:
            query["runtime_epoch"] = lease_epoch
        result = self.tasks.update_one(
            query,
            {"$set": {"offsets": offsets, "updated_at": utcnow()}},
        )
        return result.matched_count == 1

    def delete_task(self, tenant_id: int, task_id: str) -> bool:
        task = self.tasks.find_one_and_delete({"_id": task_id, "tenant_id": tenant_id})
        if task is None:
            return False
        self.jobs.delete_many({"task_id": task_id, "tenant_id": tenant_id})
        self.deliveries.delete_many({"task_id": task_id, "tenant_id": tenant_id})
        for asset in self.db.watermark_assets.files.find(
            {"metadata.task_id": task_id, "metadata.tenant_id": tenant_id}, {"_id": 1}
        ):
            self.fs.delete(asset["_id"])
        self.audit(tenant_id, "task.deleted", task_id=task_id)
        return True

    def put_watermark(self, tenant_id: int, task_id: str, data: bytes) -> str:
        previous_assets = [
            asset["_id"]
            for asset in self.db.watermark_assets.files.find(
                {"metadata.task_id": task_id, "metadata.tenant_id": tenant_id},
                {"_id": 1},
            )
        ]
        asset_id = self.fs.put(
            data,
            filename=f"{task_id}.png",
            content_type="image/png",
            metadata={"tenant_id": tenant_id, "task_id": task_id},
        )
        result = self.tasks.update_one(
            {"_id": task_id, "tenant_id": tenant_id},
            {
                "$set": {"watermark_asset_id": str(asset_id), "updated_at": utcnow()},
                "$inc": {"revision": 1},
            },
        )
        if not result.matched_count:
            self.fs.delete(asset_id)
            raise KeyError(task_id)
        for previous_id in previous_assets:
            self.fs.delete(previous_id)
        return str(asset_id)

    def delete_watermark(self, tenant_id: int, task_id: str) -> None:
        assets = list(
            self.db.watermark_assets.files.find(
                {"metadata.task_id": task_id, "metadata.tenant_id": tenant_id},
                {"_id": 1},
            )
        )
        for asset in assets:
            self.fs.delete(asset["_id"])
        self.tasks.update_one(
            {"_id": task_id, "tenant_id": tenant_id},
            {
                "$set": {"watermark_asset_id": None, "updated_at": utcnow()},
                "$inc": {"revision": 1},
            },
        )

    def get_watermark(self, tenant_id: int, asset_id: str) -> Optional[bytes]:
        try:
            grid_out = self.fs.get(ObjectId(asset_id))
            if grid_out.metadata and grid_out.metadata.get("tenant_id") != tenant_id:
                return None
            return grid_out.read()
        except Exception:
            return None

    def claimable_tenant_ids(self) -> list[int]:
        task_ids = self.tasks.distinct(
            "tenant_id",
            {
                "status": {
                    "$in": [
                        TaskStatus.VALIDATING.value,
                        TaskStatus.ACTIVE.value,
                        TaskStatus.RUNNING.value,
                    ]
                }
            },
        )
        active_accounts = set(
            self.accounts.distinct("tenant_id", {"status": AccountStatus.ACTIVE.value})
        )
        now = utcnow()
        deleting = self.accounts.distinct(
            "tenant_id",
            {
                "status": AccountStatus.DELETING.value,
                "$or": [
                    {"delete_retry_at": {"$lte": now}},
                    {"delete_retry_at": {"$exists": False}},
                ],
            },
        )
        runnable = {int(value) for value in task_ids if value in active_accounts}
        return sorted(runnable | {int(value) for value in deleting})

    def claim_lease(
        self,
        tenant_id: int,
        owner: str,
        lease_seconds: int,
    ) -> Optional[Dict[str, Any]]:
        now = utcnow()
        try:
            lease = self.leases.find_one_and_update(
                {
                    "_id": tenant_id,
                    "$or": [
                        {"expires_at": {"$lte": now}},
                        {"expires_at": {"$exists": False}},
                    ],
                },
                {
                    "$set": {
                        "owner": owner,
                        "expires_at": now + timedelta(seconds=lease_seconds),
                        "updated_at": now,
                    },
                    "$inc": {"epoch": 1},
                },
                upsert=True,
                return_document=ReturnDocument.AFTER,
            )
            if lease is not None:
                self.fence_tenant_tasks(tenant_id, int(lease["epoch"]))
                self.fence_tenant_jobs(tenant_id, int(lease["epoch"]))
            return lease
        except DuplicateKeyError:
            return None

    def fence_tenant_tasks(self, tenant_id: int, lease_epoch: int) -> None:
        self.tasks.update_many(
            {
                "tenant_id": tenant_id,
                "$or": [
                    {"runtime_epoch": {"$lt": lease_epoch}},
                    {"runtime_epoch": {"$exists": False}},
                ],
            },
            {"$max": {"runtime_epoch": lease_epoch}},
        )

    def fence_tenant_jobs(self, tenant_id: int, lease_epoch: int) -> None:
        self.jobs.update_many(
            {
                "tenant_id": tenant_id,
                "status": "running",
                "lease_epoch": {"$ne": lease_epoch},
            },
            {
                "$set": {"status": "queued", "updated_at": utcnow()},
                "$unset": {"worker_id": "", "lease_epoch": ""},
            },
        )

    def lease_is_current(self, tenant_id: int, owner: str, epoch: int) -> bool:
        return self.leases.count_documents(
            {
                "_id": tenant_id,
                "owner": owner,
                "epoch": epoch,
                "expires_at": {"$gt": utcnow()},
            },
            limit=1,
        ) == 1

    def renew_lease(
        self,
        tenant_id: int,
        owner: str,
        epoch: int,
        lease_seconds: int,
    ) -> bool:
        now = utcnow()
        result = self.leases.update_one(
            {"_id": tenant_id, "owner": owner, "epoch": epoch},
            {
                "$set": {
                    "expires_at": now + timedelta(seconds=lease_seconds),
                    "updated_at": now,
                }
            },
        )
        return result.matched_count == 1

    def release_lease(self, tenant_id: int, owner: str, epoch: int) -> None:
        self.leases.update_one(
            {"_id": tenant_id, "owner": owner, "epoch": epoch},
            {
                "$set": {
                    "owner": "",
                    "expires_at": utcnow(),
                    "updated_at": utcnow(),
                }
            },
        )

    def list_runtime_tasks(self, tenant_id: int) -> list[ForwardTask]:
        statuses = [
            TaskStatus.VALIDATING.value,
            TaskStatus.ACTIVE.value,
            TaskStatus.RUNNING.value,
        ]
        return [
            self._task_model(doc)
            for doc in self.tasks.find({"tenant_id": tenant_id, "status": {"$in": statuses}})
        ]

    def create_job(
        self,
        tenant_id: int,
        task_id: str,
        *,
        kind: str,
        job_key: Optional[str] = None,
        scheduled_at: Optional[datetime] = None,
        task_revision: Optional[int] = None,
        replace_cancelled: bool = False,
    ) -> Optional[str]:
        job_id = str(uuid.uuid4())
        key = job_key or job_id
        try:
            doc = {
                "_id": job_id,
                "job_key": key,
                "tenant_id": tenant_id,
                "task_id": task_id,
                "kind": kind,
                "status": "queued",
                "task_revision": task_revision,
                "created_at": utcnow(),
                "updated_at": utcnow(),
            }
            if scheduled_at is not None:
                doc["scheduled_at"] = scheduled_at
            self.jobs.insert_one(doc)
            return job_id
        except DuplicateKeyError:
            if replace_cancelled and scheduled_at is not None:
                replaced = self.jobs.find_one_and_update(
                    {
                        "task_id": task_id,
                        "scheduled_at": scheduled_at,
                        "status": "cancelled",
                        "task_revision": {"$ne": task_revision},
                    },
                    {
                        "$set": {
                            "job_key": key,
                            "tenant_id": tenant_id,
                            "kind": kind,
                            "status": "queued",
                            "task_revision": task_revision,
                            "updated_at": utcnow(),
                        },
                        "$unset": {
                            "worker_id": "",
                            "lease_epoch": "",
                            "error": "",
                        },
                    },
                    return_document=ReturnDocument.AFTER,
                )
                return str(replaced["_id"]) if replaced else None
            return None
        return job_id

    def claim_job(self, tenant_id: int, worker_id: str, lease_epoch: int) -> Optional[dict[str, Any]]:
        if not self.lease_is_current(tenant_id, worker_id, lease_epoch):
            return None
        self.jobs.update_many(
            {
                "tenant_id": tenant_id,
                "status": "running",
                "lease_epoch": {"$ne": lease_epoch},
            },
            {
                "$set": {"status": "queued", "updated_at": utcnow()},
                "$unset": {"worker_id": "", "lease_epoch": ""},
            },
        )
        return self.jobs.find_one_and_update(
            {"tenant_id": tenant_id, "status": "queued"},
            {
                "$set": {
                    "status": "running",
                    "worker_id": worker_id,
                    "lease_epoch": lease_epoch,
                    "updated_at": utcnow(),
                }
            },
            sort=[("created_at", 1)],
            return_document=ReturnDocument.AFTER,
        )

    def finish_job(
        self,
        job_id: str,
        *,
        worker_id: str,
        lease_epoch: int,
        error: str = "",
    ) -> bool:
        job = self.jobs.find_one({"_id": job_id}, {"tenant_id": 1})
        if job is None or not self.lease_is_current(
            int(job["tenant_id"]),
            worker_id,
            lease_epoch,
        ):
            return False
        result = self.jobs.update_one(
            {
                "_id": job_id,
                "status": "running",
                "worker_id": worker_id,
                "lease_epoch": lease_epoch,
            },
            {
                "$set": {
                    "status": "error" if error else "completed",
                    "error": error[:2000],
                    "updated_at": utcnow(),
                }
            },
        )
        return result.matched_count == 1

    def cancel_job(self, job_id: str, *, worker_id: str, lease_epoch: int) -> bool:
        job = self.jobs.find_one({"_id": job_id}, {"tenant_id": 1})
        if job is None or not self.lease_is_current(
            int(job["tenant_id"]),
            worker_id,
            lease_epoch,
        ):
            return False
        result = self.jobs.update_one(
            {
                "_id": job_id,
                "status": "running",
                "worker_id": worker_id,
                "lease_epoch": lease_epoch,
            },
            {"$set": {"status": "cancelled", "updated_at": utcnow()}},
        )
        return result.matched_count == 1

    def claim_delivery(
        self,
        delivery_key: str,
        tenant_id: int,
        task_id: str,
        *,
        source_peer_id: Optional[int] = None,
        source_message_ids: Optional[list[int]] = None,
        destination_peer_id: Optional[int] = None,
    ) -> bool:
        now = utcnow()
        reclaimed = self.deliveries.find_one_and_update(
            {
                "delivery_key": delivery_key,
                "state": "sending",
                "claim_expires_at": {"$lte": now},
            },
            {
                "$set": {
                    "tenant_id": tenant_id,
                    "task_id": task_id,
                    "source_peer_id": source_peer_id,
                    "source_message_ids": source_message_ids or [],
                    "destination_peer_id": destination_peer_id,
                    "claim_expires_at": now + timedelta(minutes=5),
                    "updated_at": now,
                }
            },
            return_document=ReturnDocument.AFTER,
        )
        if reclaimed is not None:
            return True
        try:
            self.deliveries.insert_one(
                {
                    "delivery_key": delivery_key,
                    "tenant_id": tenant_id,
                    "task_id": task_id,
                    "source_peer_id": source_peer_id,
                    "source_message_ids": source_message_ids or [],
                    "destination_peer_id": destination_peer_id,
                    "state": "sending",
                    "claim_expires_at": now + timedelta(minutes=5),
                    "created_at": now,
                    "expires_at": expiry(days=7),
                }
            )
            return True
        except DuplicateKeyError:
            return False

    def release_delivery(self, delivery_key: str) -> None:
        self.deliveries.delete_one({"delivery_key": delivery_key, "state": "sending"})

    def find_sent_message(
        self,
        tenant_id: int,
        task_id: str,
        source_peer_id: int,
        source_message_id: int,
        destination_peer_id: int,
    ) -> Optional[int]:
        doc = self.deliveries.find_one(
            {
                "tenant_id": tenant_id,
                "task_id": task_id,
                "source_peer_id": source_peer_id,
                "source_message_ids": source_message_id,
                "destination_peer_id": destination_peer_id,
                "state": "sent",
            },
            {"sent_message_id": 1, "message_map": 1},
            sort=[("created_at", -1)],
        )
        value = None
        if doc:
            value = (doc.get("message_map") or {}).get(str(source_message_id))
            if value is None:
                value = doc.get("sent_message_id")
        return int(value) if value is not None else None

    def mark_delivery_sent(
        self,
        delivery_key: str,
        sent_message_id: Optional[int],
        *,
        message_map: Optional[dict[str, int]] = None,
    ) -> None:
        self.deliveries.update_one(
            {"delivery_key": delivery_key},
            {
                "$set": {
                    "state": "sent",
                    "sent_message_id": sent_message_id,
                    "message_map": message_map or {},
                    "updated_at": utcnow(),
                },
                "$unset": {"claim_expires_at": ""},
            },
        )

    def audit(self, tenant_id: int, event: str, **metadata: Any) -> None:
        self.audit_events.insert_one(
            {
                "tenant_id": tenant_id,
                "event": event,
                "metadata": metadata,
                "created_at": utcnow(),
            }
        )

    def list_system_errors(self, *, limit: int = 100) -> list[dict[str, Any]]:
        errors = []
        for account in self.accounts.find(
            {"last_error": {"$nin": ["", None]}},
            {"_id": 0, "tenant_id": 1, "last_error": 1, "updated_at": 1},
        ):
            errors.append({"type": "account", **account})
        for task in self.tasks.find(
            {"last_error": {"$nin": ["", None]}},
            {"tenant_id": 1, "last_error": 1, "updated_at": 1},
        ):
            errors.append({"type": "task", "task_id": str(task.pop("_id")), **task})
        for job in self.jobs.find(
            {"status": "error"},
            {"tenant_id": 1, "task_id": 1, "error": 1, "updated_at": 1},
        ):
            errors.append({"type": "job", "job_id": str(job.pop("_id")), **job})
        errors.sort(key=lambda value: value.get("updated_at") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        return errors[:limit]

    def claim_status_notification(
        self,
        owner: str,
        *,
        claim_seconds: int = 60,
    ) -> Optional[dict[str, Any]]:
        now = utcnow()
        return self.audit_events.find_one_and_update(
            {
                "event": {"$in": ["account.error", "task.error", "task.completed"]},
                "notified_at": {"$exists": False},
                "$or": [
                    {"notification_claim_expires_at": {"$lte": now}},
                    {"notification_claim_expires_at": {"$exists": False}},
                ],
            },
            {
                "$set": {
                    "notification_claim_owner": owner,
                    "notification_claim_expires_at": now + timedelta(seconds=claim_seconds),
                }
            },
            sort=[("created_at", 1)],
            return_document=ReturnDocument.AFTER,
        )

    def confirm_status_notification(self, notification_id: ObjectId, owner: str) -> bool:
        result = self.audit_events.update_one(
            {
                "_id": notification_id,
                "notification_claim_owner": owner,
                "notified_at": {"$exists": False},
            },
            {
                "$set": {"notified_at": utcnow()},
                "$unset": {
                    "notification_claim_owner": "",
                    "notification_claim_expires_at": "",
                    "notification_last_error": "",
                },
            },
        )
        return result.matched_count == 1

    def release_status_notification(
        self,
        notification_id: ObjectId,
        owner: str,
        *,
        error: str = "",
    ) -> bool:
        result = self.audit_events.update_one(
            {
                "_id": notification_id,
                "notification_claim_owner": owner,
                "notified_at": {"$exists": False},
            },
            {
                "$set": {
                    "notification_last_error": error[:500],
                    "notification_last_attempt_at": utcnow(),
                },
                "$inc": {"notification_attempts": 1},
                "$unset": {
                    "notification_claim_owner": "",
                    "notification_claim_expires_at": "",
                },
            },
        )
        return result.matched_count == 1


    def register_web_tenant(
        self,
        username: str,
        password: str,
        *,
        display_name: str = "",
        tenant_id: Optional[int] = None,
        max_tasks: int = 10,
        allow_heavy_plugins: bool = True,
        expire_at: Optional[datetime] = None,
    ) -> int:
        clean_user = username.strip()
        existing = self.tenants.find_one({"web_username": clean_user.lower()})
        if existing is not None:
            raise ValueError("该用户名已被注册")
        now = utcnow()
        tid = int(tenant_id) if tenant_id else int(now.timestamp() * 1000) % 900_000_000 + 100_000_000
        if self.tenants.find_one({"_id": tid}) is not None and tenant_id is None:
            tid += 1
        self.tenants.update_one(
            {"_id": tid},
            {
                "$set": {
                    "web_username": clean_user.lower(),
                    "username": clean_user,
                    "display_name": display_name.strip() or clean_user,
                    "password_hash": hash_password(password),
                    "updated_at": now,
                },
                "$setOnInsert": {"created_at": now, "status": "active", "is_admin": False, "max_tasks": max_tasks, "allow_heavy_plugins": allow_heavy_plugins, "expire_at": expire_at},
            },
            upsert=True,
        )
        self.audit(tid, "tenant.registered", username=clean_user)
        return tid

    def authenticate_web_tenant(self, username: str, password: str) -> Optional[int]:
        clean = username.strip()
        query: dict[str, Any] = {"web_username": clean.lower()}
        if clean.isdigit():
            query = {"$or": [{"web_username": clean.lower()}, {"_id": int(clean)}]}
        doc = self.tenants.find_one(query)
        if not doc or doc.get("status") == "disabled":
            return None
        if not verify_password(password, str(doc.get("password_hash", ""))):
            return None
        return int(doc["_id"])

    def list_all_tenants_detailed(self) -> list[dict[str, Any]]:
        tenants_list = []
        accounts_map = {
            int(a["tenant_id"]): a
            for a in self.accounts.find({}, {"_id": 0, "encrypted_session": 0})
        }
        all_tasks = [self._task_model(doc) for doc in self.tasks.find({}).sort("created_at", 1)]
        tasks_by_tenant: dict[int, list[dict[str, Any]]] = {}
        for t in all_tasks:
            tasks_by_tenant.setdefault(int(t.tenant_id), []).append(t.model_dump(mode="json"))

        seen_ids = set()
        for doc in self.tenants.find({}).sort("created_at", -1):
            tid = int(doc["_id"])
            seen_ids.add(tid)
            acc = accounts_map.get(tid)
            t_list = tasks_by_tenant.get(tid, [])
            tenants_list.append({
                "tenant_id": tid,
                "username": doc.get("username") or (acc.get("username") if acc else "") or f"user_{tid}",
                "display_name": doc.get("display_name") or (acc.get("display_name") if acc else ""),
                "status": doc.get("status", "active"),
                "is_admin": bool(doc.get("is_admin", False)),
                "max_tasks": int(doc.get("max_tasks", 10)),
                "allow_heavy_plugins": bool(doc.get("allow_heavy_plugins", True)),
                "expire_at": doc.get("expire_at"),
                "account": acc,
                "task_count": len(t_list),
                "active_task_count": sum(1 for x in t_list if x.get("status") in {"active", "running"}),
                "tasks": t_list,
                "updated_at": doc.get("updated_at"),
            })

        # 补充仅在 tasks 或 accounts 中存在但未写入 tenants 表的租户
        for tid in (set(accounts_map.keys()) | set(tasks_by_tenant.keys())) - seen_ids:
            acc = accounts_map.get(tid)
            t_list = tasks_by_tenant.get(tid, [])
            tenants_list.append({
                "tenant_id": tid,
                "username": (acc.get("username") if acc else "") or f"user_{tid}",
                "display_name": (acc.get("display_name") if acc else ""),
                "status": "active",
                "is_admin": False,
                "account": acc,
                "task_count": len(t_list),
                "active_task_count": sum(1 for x in t_list if x.get("status") in {"active", "running"}),
                "tasks": t_list,
                "updated_at": utcnow(),
            })
        return tenants_list

    def update_tenant_by_admin(
        self,
        tenant_id: int,
        *,
        username: Optional[str] = None,
        display_name: Optional[str] = None,
        status: Optional[str] = None,
        is_admin: Optional[bool] = None,
        new_password: Optional[str] = None,
        max_tasks: Optional[int] = None,
        allow_heavy_plugins: Optional[bool] = None,
        expire_at: Optional[datetime] = None,
    ) -> dict[str, Any]:
        updates: dict[str, Any] = {"updated_at": utcnow()}
        if username is not None and username.strip():
            updates["username"] = username.strip()
            updates["web_username"] = username.strip().lower()
        if display_name is not None:
            updates["display_name"] = display_name.strip()
        if status in {"active", "disabled"}:
            updates["status"] = status
            if status == "disabled":
                self.tasks.update_many(
                    {"tenant_id": tenant_id},
                    {"$set": {"status": TaskStatus.PAUSED.value, "pending_action": None, "updated_at": utcnow()}},
                )
        if is_admin is not None:
            updates["is_admin"] = bool(is_admin)
        if new_password:
            updates["password_hash"] = hash_password(new_password)
        if max_tasks is not None:
            updates["max_tasks"] = int(max_tasks)
        if allow_heavy_plugins is not None:
            updates["allow_heavy_plugins"] = bool(allow_heavy_plugins)
        if expire_at is not None:
            updates["expire_at"] = expire_at
        self.tenants.update_one(
            {"_id": tenant_id},
            {"$set": updates, "$setOnInsert": {"created_at": utcnow()}},
            upsert=True,
        )
        doc = self.tenants.find_one({"_id": tenant_id}) or {"_id": tenant_id}
        doc["tenant_id"] = doc.pop("_id")
        doc.pop("password_hash", None)
        return doc


    def get_admin_settings(
        self,
        *,
        default_username: str = "admin",
        default_password: str = "admin",
        default_path: str = "/admin",
    ) -> dict[str, Any]:
        doc = self.db.system_settings.find_one({"_id": "admin_config"}) or {}
        raw_path = str(doc.get("admin_path") or default_path).strip()
        if not raw_path.startswith("/"):
            raw_path = "/" + raw_path
        raw_path = raw_path.rstrip("/") or "/admin"
        return {
            "admin_username": str(doc.get("admin_username") or default_username).strip(),
            "admin_password_hash": doc.get("admin_password_hash") or hash_password(default_password),
            "admin_path": raw_path,
        }

    def update_admin_settings(
        self,
        *,
        admin_username: Optional[str] = None,
        admin_password: Optional[str] = None,
        admin_path: Optional[str] = None,
    ) -> dict[str, Any]:
        updates: dict[str, Any] = {"updated_at": utcnow()}
        if admin_username and admin_username.strip():
            updates["admin_username"] = admin_username.strip()
        if admin_password:
            updates["admin_password_hash"] = hash_password(admin_password)
        if admin_path and admin_path.strip():
            p = admin_path.strip()
            if not p.startswith("/"):
                p = "/" + p
            p = p.rstrip("/") or "/admin"
            updates["admin_path"] = p
        self.db.system_settings.update_one(
            {"_id": "admin_config"},
            {"$set": updates, "$setOnInsert": {"created_at": utcnow()}},
            upsert=True,
        )
        cfg = self.get_admin_settings()
        return {
            "admin_username": cfg["admin_username"],
            "admin_path": cfg["admin_path"],
        }


    def save_phone_login_challenge(
        self,
        tenant_id: int,
        *,
        phone: str,
        phone_code_hash: str,
        encrypted_temp_session: str,
    ) -> None:
        self.db.login_challenges.update_one(
            {"_id": tenant_id},
            {
                "$set": {
                    "phone": phone,
                    "phone_code_hash": phone_code_hash,
                    "encrypted_temp_session": encrypted_temp_session,
                    "updated_at": utcnow(),
                }
            },
            upsert=True,
        )

    def get_phone_login_challenge(self, tenant_id: int) -> Optional[dict[str, Any]]:
        return self.db.login_challenges.find_one({"_id": tenant_id})

    def clear_phone_login_challenge(self, tenant_id: int) -> None:
        self.db.login_challenges.delete_one({"_id": tenant_id})

    def list_recent_deliveries(
        self,
        tenant_id: Optional[int] = None,
        *,
        limit: int = 30,
    ) -> list[dict[str, Any]]:
        query: dict[str, Any] = {}
        if tenant_id is not None:
            query["tenant_id"] = tenant_id
        rows = []
        for doc in self.deliveries.find(query).sort("updated_at", -1).limit(limit):
            rows.append({
                "delivery_key": doc.get("delivery_key", ""),
                "tenant_id": doc.get("tenant_id"),
                "task_id": doc.get("task_id", ""),
                "source_peer_id": doc.get("source_peer_id"),
                "source_message_ids": doc.get("source_message_ids", []),
                "destination_peer_id": doc.get("destination_peer_id"),
                "sent_message_id": doc.get("sent_message_id"),
                "state": doc.get("state", "sent"),
                "updated_at": doc.get("updated_at").isoformat() if doc.get("updated_at") else "",
            })
        return rows

    @staticmethod
    def _task_model(doc: Dict[str, Any]) -> ForwardTask:
        value = dict(doc)
        value["id"] = str(value.pop("_id"))
        return ForwardTask.model_validate(value)
