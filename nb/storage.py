import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
import logging

from pymongo.collection import Collection
from telethon.tl.custom.message import Message

from nb.const import FAILED_FORWARD_LOG_FILE_NAME


class EventUid:
    """Uniquely identifies a message with its chat id and message id."""

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
        return hash((self.chat_id, self.msg_id))


class DummyEvent:
    def __init__(self, chat_id, msg_id):
        self.chat_id = chat_id
        self.msg_id = msg_id


stored: Dict[EventUid, Dict[int, Message]] = {}
CONFIG_TYPE: int = 0
mycol: Optional[Collection] = None

post_id_mapping: Dict[tuple, Dict[int, int]] = {}
discussion_to_channel_post: Dict[tuple, int] = {}
comment_msg_mapping: Dict[tuple, Dict[int, int]] = {}
KEEP_LAST_MANY_POSTS = 50000


def add_post_mapping(src_channel_id: int, src_post_id: int, dest_channel_id: int, dest_post_id: int) -> None:
    key = (src_channel_id, src_post_id)
    if key not in post_id_mapping:
        post_id_mapping[key] = {}
    post_id_mapping[key][dest_channel_id] = dest_post_id
    if len(post_id_mapping) > KEEP_LAST_MANY_POSTS:
        oldest_key = next(iter(post_id_mapping))
        del post_id_mapping[oldest_key]


def get_dest_post_id(src_channel_id: int, src_post_id: int, dest_channel_id: int) -> Optional[int]:
    return post_id_mapping.get((src_channel_id, src_post_id), {}).get(dest_channel_id)


def add_comment_mapping(src_discussion_id: int, src_comment_id: int, dest_chat_id: int, dest_msg_id: int) -> None:
    key = (src_discussion_id, src_comment_id)
    if key not in comment_msg_mapping:
        comment_msg_mapping[key] = {}
    comment_msg_mapping[key][dest_chat_id] = dest_msg_id


def get_comment_dest(src_discussion_id: int, src_comment_id: int) -> Optional[Dict[int, int]]:
    return comment_msg_mapping.get((src_discussion_id, src_comment_id))


def log_failed_forward(record: Dict[str, Any]) -> None:
    record.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
    with open(FAILED_FORWARD_LOG_FILE_NAME, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
