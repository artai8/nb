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
        except Exception:
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

# =====================================================================
#  帖子 ID 映射（评论区功能核心）
# =====================================================================

# 源频道帖子 → 目标频道帖子的映射
# 结构: { (src_channel_id, src_post_id): { dest_channel_id: dest_post_id } }
post_id_mapping: Dict[tuple, Dict[int, int]] = {}

# 讨论组消息 → 对应的频道帖子 ID
# 结构: { (discussion_group_id, reply_to_top_id): src_channel_post_id }
# Telegram 评论区消息的 reply_to.reply_to_top_id 指向讨论组中的"频道帖子副本"
discussion_to_channel_post: Dict[tuple, int] = {}

# 评论消息的映射（用于编辑/删除同步）
# 结构: { (src_discussion_group_id, comment_msg_id): { dest_chat_id: dest_msg_id } }
comment_msg_mapping: Dict[tuple, Dict[int, int]] = {}

KEEP_LAST_MANY_POSTS = 50000  # 帖子映射保留数量


def add_post_mapping(
    src_channel_id: int,
    src_post_id: int,
    dest_channel_id: int,
    dest_post_id: int,
) -> None:
    """记录帖子映射: 源频道帖子 → 目标频道帖子"""
    key = (src_channel_id, src_post_id)
    if key not in post_id_mapping:
        post_id_mapping[key] = {}
    post_id_mapping[key][dest_channel_id] = dest_post_id
    logging.info(
        f"📌 帖子映射: src({src_channel_id}, {src_post_id}) "
        f"→ dest({dest_channel_id}, {dest_post_id})"
    )

    # 自动清理过旧的映射
    if len(post_id_mapping) > KEEP_LAST_MANY_POSTS:
        oldest_key = next(iter(post_id_mapping))
        del post_id_mapping[oldest_key]


def get_dest_post_id(
    src_channel_id: int,
    src_post_id: int,
    dest_channel_id: int,
) -> Optional[int]:
    """查询目标频道中对应的帖子 ID"""
    key = (src_channel_id, src_post_id)
    mapping = post_id_mapping.get(key, {})
    return mapping.get(dest_channel_id)


def add_comment_mapping(
    src_discussion_id: int,
    src_comment_id: int,
    dest_chat_id: int,
    dest_msg_id: int,
) -> None:
    """记录评论消息的映射"""
    key = (src_discussion_id, src_comment_id)
    if key not in comment_msg_mapping:
        comment_msg_mapping[key] = {}
    comment_msg_mapping[key][dest_chat_id] = dest_msg_id


def get_comment_dest(
    src_discussion_id: int,
    src_comment_id: int,
) -> Optional[Dict[int, int]]:
    """查询评论在目标的映射"""
    key = (src_discussion_id, src_comment_id)
    return comment_msg_mapping.get(key)


# =====================================================================
#  媒体组临时缓存与超时管理（保持不变）
# =====================================================================
GROUPED_CACHE: Dict[int, Dict[int, List[Message]]] = {}
GROUPED_TIMERS: Dict[int, asyncio.TimerHandle] = {}
GROUPED_TIMEOUT = 1.5
GROUPED_MAPPING: Dict[int, Dict[int, List[int]]] = {}


async def _flush_group(grouped_id: int) -> None:
    """超时或组完整时发送缓存中的媒体组"""
    if grouped_id not in GROUPED_CACHE:
        return
    try:
        from nb.live import _send_grouped_messages
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

    if grouped_id in GROUPED_TIMERS:
        GROUPED_TIMERS[grouped_id].cancel()

    loop = asyncio.get_running_loop()
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
