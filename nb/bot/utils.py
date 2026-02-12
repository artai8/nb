import logging
from typing import List
from telethon import events
from nb import config
from nb.config import Forward


def admin_protect(org_func):
    async def wrapper_func(event):
        if event.sender_id not in config.ADMINS:
            await event.respond("无权操作")
            raise events.StopPropagation
        return await org_func(event)
    return wrapper_func


def get_args(text):
    splitted = text.split(" ", 1)
    if len(splitted) != 2:
        splitted = text.split("\n", 1)
        if len(splitted) != 2:
            return ""
    return splitted[1].strip()


def display_forwards(forwards):
    if not forwards:
        return "当前没有转发配置"
    s = "当前配置:"
    for f in forwards:
        s += f"\n\n```\nsource: {f.source}\ndest: {f.dest}\n```\n"
    return s


def remove_source(source, forwards):
    for i, f in enumerate(forwards):
        if f.source == source:
            del forwards[i]
            return forwards
    raise ValueError("源不存在")


def get_command_prefix():
    if config.is_bot is None:
        raise ValueError("config.is_bot 未设置")
    return "/" if config.is_bot else r"\."
