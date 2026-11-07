"""Helper functions for the bot."""

import logging
from typing import List
from telethon import events
from nb import config
from nb.config import Forward


def admin_protect(org_func):
    async def wrapper_func(event):
        if not config.ADMINS:
            await event.respond("管理员列表未配置。")
            raise events.StopPropagation
        if event.sender_id not in config.ADMINS:
            await event.respond("无权限执行此操作。")
            raise events.StopPropagation
        return await org_func(event)
    return wrapper_func


def get_args(text: str) -> str:
    splitted = text.split(" ", 1)
    if len(splitted) != 2:
        splitted = text.split("\n", 1)
        if len(splitted) != 2:
            return ""
    return splitted[1].strip()


def display_forwards(forwards: List[Forward]) -> str:
    if not forwards:
        return "当前暂无转发规则"
    return "\n".join(f"sources: {f.sources} -> dest: {f.dest}" for f in forwards)
