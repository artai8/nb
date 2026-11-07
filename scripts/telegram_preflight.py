"""Read-only Telegram preflight with an opt-in send/delete probe."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
from pathlib import Path

from telethon import TelegramClient
from telethon.sessions import StringSession


def secret_value(name: str) -> str:
    value = os.getenv(name, "").strip()
    file_name = os.getenv(f"{name}_FILE", "").strip()
    if not value and file_name:
        value = Path(file_name).read_text(encoding="utf-8").strip()
    if not value:
        raise RuntimeError(f"Missing {name} or {name}_FILE")
    return value


async def run() -> None:
    api_id = int(secret_value("API_ID"))
    api_hash = secret_value("API_HASH")
    session = secret_value("TELEGRAM_TEST_SESSION")
    source = secret_value("TELEGRAM_TEST_SOURCE")
    destination = secret_value("TELEGRAM_TEST_DESTINATION")
    client = TelegramClient(StringSession(session), api_id, api_hash)
    await client.connect()
    try:
        me = await client.get_me()
        source_entity = await client.get_entity(source)
        destination_entity = await client.get_entity(destination)
        print(json.dumps({"account_id": int(me.id), "source_id": int(source_entity.id), "destination_id": int(destination_entity.id)}))
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(run())
