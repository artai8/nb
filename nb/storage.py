from typing import Dict, List, Optional
import asyncio
import logging

from pymongo.collection import Collection
from telethon.tl.custom.message import Message


class EventUid:
    """The objects of this class uniquely identifies a message with its chat id and message id."""

    def __init__(self, event) -> None:
        self.chat_id = event.chat_id
        try:
            self.msg_id = event.id
        except:  # pylint: disable=bare-except
            self.msg_id = event.deleted_id

    def __str__(self) -> str:
        return f"chat={self.chat_id} msg={self.msg_id}"

    def __eq__(self, other) -> bool:
        return self.chat_id == other.chat_id and self.msg_id == other.msg_id

    def __hash__(self) -> int:
        return hash(self.__str__())


class DummyEvent:
    def __init__(self, chat_id, msg_id):
        self.chat_id = chat_id
        self.id = msg_id


stored: Dict[EventUid, Dict[int, Message]] = {}
CONFIG_TYPE: int = 0
mycol: Collection = None

# 媒体组临时缓存与超时管理
GROUPED_CACHE: Dict[int, Dict[int, List[Message]]] = {}  # grouped_id -> {chat_id: [messages]}
GROUPED_TIMERS: Dict[int, asyncio.TimerHandle] = {}  # 修复：正确的类型标注
GROUPED_TIMEOUT = 1.5  # 秒，等待同组其他消息的超时时间
GROUPED_MAPPING: Dict[int, Dict[int, List[int]]] = {}  # grouped_id -> {chat_id: [msg_ids]}


async def _flush_group(grouped_id: int) -> None:
    """超时或组完整时发送缓存中的媒体组"""
    if grouped_id not in GROUPED_CACHE:
        return
    try:
        from nb.live import _send_grouped_messages  # 避免循环导入
        await _send_grouped_messages(grouped_id)
    except Exception as e:
        logging.exception(
            f"Failed to send grouped messages for grouped_id={grouped_id}: {e}"
        )
    finally:
        GROUPED_CACHE.pop(grouped_id, None)
        GROUPED_TIMERS.pop(grouped_id, None)


def add_to_group_cache(chat_id: int, grouped_id: int, message: Message) -> None:
    """将消息加入媒体组缓存，并启动/重置超时定时器"""
    if grouped_id not in GROUPED_CACHE:
        GROUPED_CACHE[grouped_id] = {}
        GROUPED_MAPPING[grouped_id] = {}
    if chat_id not in GROUPED_CACHE[grouped_id]:
        GROUPED_CACHE[grouped_id][chat_id] = []
        GROUPED_MAPPING[grouped_id][chat_id] = []
    GROUPED_CACHE[grouped_id][chat_id].append(message)
    GROUPED_MAPPING[grouped_id][chat_id].append(message.id)

    # 重置定时器
    if grouped_id in GROUPED_TIMERS:
        GROUPED_TIMERS[grouped_id].cancel()

    loop = asyncio.get_running_loop()
    # 修复：用默认参数捕获当前 grouped_id，避免闭包晚绑定
    GROUPED_TIMERS[grouped_id] = loop.call_later(
        GROUPED_TIMEOUT,
        lambda gid=grouped_id: asyncio.ensure_future(_flush_group(gid)),
    )


def get_grouped_messages(chat_id: int, msg_id: int) -> Optional[List[int]]:
    """根据消息ID获取同组所有消息ID"""
    for grouped_id, mapping in GROUPED_MAPPING.items():
        if chat_id in mapping and msg_id in mapping[chat_id]:
            return mapping[chat_id]
    return None
