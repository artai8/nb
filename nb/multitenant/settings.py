"""Runtime configuration for multi-tenant services."""

from __future__ import annotations

import os
from typing import Set

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _parse_admin_ids(v: object) -> Set[int]:
    if isinstance(v, set):
        return {int(x) for x in v}
    if isinstance(v, (list, tuple)):
        return {int(x) for x in v}
    if isinstance(v, str):
        return {int(x.strip()) for x in v.split(",") if x.strip()}
    return set()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="NB_",
        env_file=".env",
        extra="ignore",
    )

    api_id: int = 0
    api_hash: str = ""
    bot_token: str = ""
    session_encryption_key: str = ""
    mongo_uri: str = "mongodb://localhost:27017"
    mongo_db: str = "nb_multitenant"
    worker_id: str = Field(default_factory=lambda: f"worker-{os.getpid()}")
    public_base_url: str = ""
    admin_ids: Set[int] = Field(default_factory=set)
    lease_seconds: int = 30
    worker_capacity: int = 20
    poll_seconds: float = 3.0
    sync_interval_seconds: float = 5.0
    # 0 表示取消登录链接与会话的 5 分钟/固定时间限制（永久有效，直到主动登出）
    magic_link_ttl_minutes: int = 0
    magic_link_Reusable: bool = True
    web_session_hours: int = 87600  # 默认 10 年 (3650天)
    admin_username: str = "admin"
    admin_password: str = "admin"
    admin_path: str = "/admin"

    @field_validator("admin_ids", mode="before")
    @classmethod
    def parse_admin_ids(cls, v: object) -> Set[int]:
        return _parse_admin_ids(v)

    def validate_for_bot(self) -> None:
        missing = []
        if not self.api_id:
            missing.append("NB_API_ID")
        if not self.api_hash:
            missing.append("NB_API_HASH")
        if not self.bot_token:
            missing.append("NB_BOT_TOKEN")
        if not self.session_encryption_key:
            missing.append("NB_SESSION_ENCRYPTION_KEY")
        if missing:
            raise ValueError(f"Missing required bot settings: {', '.join(missing)}")

    def validate_for_api(self) -> None:
        missing = []
        if not self.bot_token:
            missing.append("NB_BOT_TOKEN")
        if missing:
            raise ValueError(f"Missing required API settings: {', '.join(missing)}")

    def validate_for_worker(self) -> None:
        self.validate_for_bot()

    @classmethod
    def for_bot(cls) -> "Settings":
        cfg = cls()
        cfg.validate_for_bot()
        return cfg

    @classmethod
    def for_api(cls) -> "Settings":
        cfg = cls()
        cfg.validate_for_api()
        return cfg

    @classmethod
    def for_worker(cls) -> "Settings":
        cfg = cls()
        cfg.validate_for_worker()
        return cfg
