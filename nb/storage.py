import asyncio
import logging
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, field
from pymongo.collection import Collection
from telethon.tl.custom.message import Message


class EventUid:
    def __init__(self, event):
        self.chat_id = event.chat_id
        try:
            self.msg_id = event.id
        except Exception:
            self.msg_id = event.deleted_id

    def __str__(self):
        return f"chat={self.chat_id} msg={self.msg_id}"

    def __eq__(self, other):
        return self.chat_id == other.chat_id and self.msg_id == other.msg_id

    def __hash__(self):
        return hash(self.__str__())


class DummyEvent:
    def __init__(self, chat_id, msg_id):
        self.chat_id = chat_id
        self.id = msg_id


@dataclass
class PendingComment:
    message: Message
    forward: Any
    source_channel_id: int
    source_post_id: int
    attempts: int = 0
    grouped_id: Optional[int] = None


stored: Dict[EventUid, Dict[int, Message]] = {}
CONFIG_TYPE: int = 0
mycol: Collection = None
post_id_mapping: Dict[tuple, Dict[int, int]] = {}
discussion_to_channel_post: Dict[tuple, int] = {}
channel_post_to_discussion: Dict[tuple, int] = {}
comment_msg_mapping: Dict[tuple, Dict[int, int]] = {}
KEEP_LAST_MANY_POSTS = 50000
GROUPED_CACHE: Dict[int, Dict[int, List[Message]]] = {}
GROUPED_TIMERS: Dict[int, asyncio.TimerHandle] = {}
GROUPED_TIMEOUT = 1.5
GROUPED_MAPPING: Dict[int, Dict[int, List[int]]] = {}

# 评论相关存储
PENDING_COMMENTS: Dict[Tuple[int, int], List[PendingComment]] = {}
PENDING_COMMENT_GROUPS: Dict[int, List[Message]] = {}
PENDING_COMMENT_LOCK = asyncio.Lock()
PROCESSED_COMMENTS: set = set()


def add_post_mapping(src_channel_id, src_post_id, dest_channel_id, dest_post_id):
    key = (src_channel_id, src_post_id)
    if key not in post_id_mapping:
        post_id_mapping[key] = {}
    post_id_mapping[key][dest_channel_id] = dest_post_id
    if len(post_id_mapping) > KEEP_LAST_MANY_POSTS:
        del post_id_mapping[next(iter(post_id_mapping))]
    logging.debug(f"建立帖子映射: {src_channel_id}/{src_post_id} -> {dest_channel_id}/{dest_post_id}")


def get_dest_post_id(src_channel_id, src_post_id, dest_channel_id):
    return post_id_mapping.get((src_channel_id, src_post_id), {}).get(dest_channel_id)


def add_discussion_mapping(discussion_id, discussion_msg_id, channel_post_id):
    discussion_to_channel_post[(discussion_id, discussion_msg_id)] = channel_post_id
    channel_post_to_discussion[(discussion_id, channel_post_id)] = discussion_msg_id
    if len(discussion_to_channel_post) > KEEP_LAST_MANY_POSTS:
        oldest = next(iter(discussion_to_channel_post))
        old_cp = discussion_to_channel_post.pop(oldest)
        channel_post_to_discussion.pop((oldest[0], old_cp), None)


def get_channel_post_id(discussion_id, discussion_msg_id):
    return discussion_to_channel_post.get((discussion_id, discussion_msg_id))


def get_discussion_msg_id(discussion_id, channel_post_id):
    return channel_post_to_discussion.get((discussion_id, channel_post_id))


def add_comment_mapping(src_discussion_id, src_comment_id, dest_chat_id, dest_msg_id):
    key = (src_discussion_id, src_comment_id)
    if key not in comment_msg_mapping:
        comment_msg_mapping[key] = {}
    comment_msg_mapping[key][dest_chat_id] = dest_msg_id
    logging.debug(f"建立评论映射: {src_discussion_id}/{src_comment_id} -> {dest_chat_id}/{dest_msg_id}")


def get_comment_dest(src_discussion_id, src_comment_id):
    return comment_msg_mapping.get((src_discussion_id, src_comment_id))


async def _flush_group(grouped_id):
    if grouped_id not in GROUPED_CACHE:
        return
    try:
        from nb.live import _send_grouped_messages
        await _send_grouped_messages(grouped_id)
    except Exception as e:
        logging.exception(f"发送媒体组失败 grouped_id={grouped_id}: {e}")
    finally:
        GROUPED_CACHE.pop(grouped_id, None)
        GROUPED_TIMERS.pop(grouped_id, None)


def add_to_group_cache(chat_id, grouped_id, message):
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


def get_grouped_messages(chat_id, msg_id):
    for grouped_id, mapping in GROUPED_MAPPING.items():
        if chat_id in mapping and msg_id in mapping[chat_id]:
            return mapping[chat_id]
    return None
