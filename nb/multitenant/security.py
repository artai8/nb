"""Cryptographic helpers, token utilities, and Telegram proxy configuration."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import parse_qsl

from cryptography.fernet import Fernet, InvalidToken


@dataclass
class MiniAppUser:
    id: int
    first_name: str = ""
    last_name: str = ""
    username: str = ""


def expiry(
    *,
    days: int = 0,
    hours: int = 0,
    minutes: int = 0,
    seconds: int = 0,
) -> datetime:
    """Return a UTC expiration timestamp offset from current time."""
    if days == 0 and hours == 0 and minutes == 0 and seconds == 0:
        # 取消时间限制时，返回 100 年后的远期 UTC 时间，避免 MongoDB TTL 索引自动清理
        return datetime.now(timezone.utc) + timedelta(days=36500)
    return datetime.now(timezone.utc) + timedelta(
        days=days, hours=hours, minutes=minutes, seconds=seconds
    )


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class SessionCipher:
    def __init__(self, secret_key: Optional[str] = None) -> None:
        key_raw = secret_key or os.getenv("NB_SESSION_ENCRYPTION_KEY") or "default-secret-key-change-me"
        key_bytes = hashlib.sha256(key_raw.encode("utf-8")).digest()[:32]
        self.fernet = Fernet(base64.urlsafe_b64encode(key_bytes))

    def encrypt(self, plain_text: str) -> str:
        if not plain_text:
            return ""
        return self.fernet.encrypt(plain_text.encode("utf-8")).decode("utf-8")

    def decrypt(self, cipher_text: str) -> str:
        if not cipher_text:
            return ""
        try:
            return self.fernet.decrypt(cipher_text.encode("utf-8")).decode("utf-8")
        except InvalidToken:
            return ""


def validate_miniapp_init_data(init_data: str, bot_token: str) -> MiniAppUser:
    """Verify Telegram Mini App initData signature and return MiniAppUser."""
    if not init_data or not bot_token:
        raise ValueError("Missing init_data or bot_token")
    parsed = dict(parse_qsl(init_data))
    hash_val = parsed.pop("hash", None)
    if not hash_val:
        raise ValueError("Missing signature hash")

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    calculated_hash = hmac.new(
        secret_key, data_check_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(calculated_hash, hash_val):
        raise ValueError("Invalid Mini App signature")

    user_raw = parsed.get("user")
    if not user_raw:
        raise ValueError("Missing user object in init_data")
    user_data = json.loads(user_raw)
    return MiniAppUser(
        id=int(user_data["id"]),
        first_name=str(user_data.get("first_name", "")),
        last_name=str(user_data.get("last_name", "")),
        username=str(user_data.get("username", "")),
    )


def get_telegram_proxy() -> Optional[tuple]:
    """Build Telethon-compatible proxy tuple when proxy is enabled."""
    proxy_enabled = os.getenv("PROXY_ENABLED", "").strip().lower()
    host = (
        os.getenv("PROXY_HOST", "").strip()
        or os.getenv("TELEGRAM_PROXY_HOST", "").strip()
    )
    port_raw = (
        os.getenv("PROXY_PORT", "").strip()
        or os.getenv("TELEGRAM_PROXY_PORT", "").strip()
    )
    if proxy_enabled in {"0", "false", "no", "off"}:
        return None
    if not host and proxy_enabled not in {"1", "true", "yes", "on"}:
        return None

    host = host or "127.0.0.1"
    try:
        port = int(port_raw or "7928")
    except ValueError:
        return None
    if port <= 0:
        return None

    proxy_type = (
        os.getenv("PROXY_TYPE", "").strip().lower()
        or os.getenv("TELEGRAM_PROXY_TYPE", "").strip().lower()
        or "socks5"
    )

    import socks

    type_map = {
        "socks5": socks.SOCKS5,
        "socks4": socks.SOCKS4,
        "http": socks.HTTP,
    }

    username = os.getenv("PROXY_USERNAME", "").strip() or os.getenv(
        "TELEGRAM_PROXY_USERNAME", ""
    ).strip()
    password = os.getenv("PROXY_PASSWORD", "").strip() or os.getenv(
        "TELEGRAM_PROXY_PASSWORD", ""
    ).strip()

    return (
        type_map.get(proxy_type, socks.SOCKS5),
        host,
        port,
        True,
        username or None,
        password or None,
    )


def hash_password(password: str, salt: Optional[str] = None) -> str:
    salt_val = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt_val.encode("utf-8"), 120_000)
    return f"{salt_val}${dk.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    if not stored_hash or "$" not in stored_hash:
        return False
    salt_val, _ = stored_hash.split("$", 1)
    expected = hash_password(password, salt=salt_val)
    return hmac.compare_digest(expected, stored_hash)
