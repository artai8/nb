from typing import Dict, List, Optional, Tuple
import asyncio
import logging

from pymongo.collection import Collection
from telethon.tl.custom.message import Message


class EventUid:
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

# ★ 帖子映射：(src_channel_id, src_post_id) → {dest_channel_id: dest_post_id}
post_id_mapping: Dict[Tuple[int, int], Dict[int, int]] = {}

# ★ 讨论组帖子副本 → 频道帖子映射：(discussion_id, discussion_msg_id) → channel_post_id
discussion_to_channel_post: Dict[Tuple[int, int], int] = {}

# ★ 评论映射：(src_discussion_id, src_comment_id) → {dest_chat_id: dest_msg_id}
comment_msg_mapping: Dict[Tuple[int, int], Dict[int, int]] = {}

KEEP_LAST_MANY_POSTS = 50000


def _normalize_channel_id(channel_id: int) -> int:
    """★ 修复：标准化频道 ID 格式
    
    Telegram 频道/超级群组的 ID 有两种格式：
    - API 返回的格式：1234567890（正数）
    - 常见格式：-1001234567890（负数，带 -100 前缀）
    
    此函数统一转换为带 -100 前缀的格式
    """
    if channel_id is None:
        return 0
    
    channel_id = int(channel_id)
    
    # 已经是负数格式
    if channel_id < 0:
        return channel_id
    
    # 正数格式，转换为 -100 前缀格式
    return int(f"-100{channel_id}")


def _get_all_id_variants(channel_id: int) -> List[int]:
    """★ 获取 ID 的所有可能变体，用于查找映射"""
    if channel_id is None:
        return []
    
    channel_id = int(channel_id)
    variants = [channel_id]
    
    if channel_id < 0:
        # 负数：添加正数变体
        abs_id = abs(channel_id)
        variants.append(abs_id)
        
        # 如果是 -100xxx 格式，提取 xxx
        str_id = str(abs_id)
        if str_id.startswith("100") and len(str_id) > 3:
            variants.append(int(str_id[3:]))
            variants.append(-int(str_id[3:]))
    else:
        # 正数：添加负数变体
        variants.append(-channel_id)
        variants.append(int(f"-100{channel_id}"))
    
    return list(set(variants))


def add_post_mapping(
    src_channel_id: int,
    src_post_id: int,
    dest_channel_id: int,
    dest_post_id: int,
) -> None:
    """★ 增强：添加帖子映射，支持 ID 格式标准化"""
    if dest_post_id is None or dest_post_id == 0:
        logging.warning(
            f"⚠️ 跳过无效的帖子映射: src({src_channel_id}, {src_post_id}) → "
            f"dest({dest_channel_id}, {dest_post_id})"
        )
        return
    
    # ★ 标准化 ID
    src_normalized = _normalize_channel_id(src_channel_id)
    dest_normalized = _normalize_channel_id(dest_channel_id)
    
    key = (src_normalized, src_post_id)
    if key not in post_id_mapping:
        post_id_mapping[key] = {}
    
    post_id_mapping[key][dest_normalized] = dest_post_id
    
    # ★ 同时保存原始 ID 的映射（兼容性）
    original_key = (src_channel_id, src_post_id)
    if original_key != key:
        if original_key not in post_id_mapping:
            post_id_mapping[original_key] = {}
        post_id_mapping[original_key][dest_channel_id] = dest_post_id
    
    logging.info(
        f"📌 帖子映射: src({src_channel_id}, {src_post_id}) → "
        f"dest({dest_channel_id}, {dest_post_id})"
    )
    
    # 清理旧映射
    if len(post_id_mapping) > KEEP_LAST_MANY_POSTS:
        oldest_key = next(iter(post_id_mapping))
        del post_id_mapping[oldest_key]


def get_dest_post_id(
    src_channel_id: int,
    src_post_id: int,
    dest_channel_id: int,
) -> Optional[int]:
    """★ 增强：获取目标帖子 ID，支持多种 ID 格式"""
    # 尝试所有可能的源 ID 变体
    src_variants = _get_all_id_variants(src_channel_id)
    dest_variants = _get_all_id_variants(dest_channel_id)
    
    for src_v in src_variants:
        key = (src_v, src_post_id)
        if key in post_id_mapping:
            mapping = post_id_mapping[key]
            
            # 尝试所有可能的目标 ID 变体
            for dest_v in dest_variants:
                if dest_v in mapping:
                    return mapping[dest_v]
    
    return None


def add_comment_mapping(
    src_discussion_id: int,
    src_comment_id: int,
    dest_chat_id: int,
    dest_msg_id: int,
) -> None:
    """添加评论映射"""
    if dest_msg_id is None:
        return
    
    key = (src_discussion_id, src_comment_id)
    if key not in comment_msg_mapping:
        comment_msg_mapping[key] = {}
    comment_msg_mapping[key][dest_chat_id] = dest_msg_id
    
    logging.debug(
        f"📝 评论映射: src({src_discussion_id}, {src_comment_id}) → "
        f"dest({dest_chat_id}, {dest_msg_id})"
    )


def get_comment_dest(
    src_discussion_id: int,
    src_comment_id: int,
) -> Optional[Dict[int, int]]:
    key = (src_discussion_id, src_comment_id)
    return comment_msg_mapping.get(key)


def add_discussion_post_mapping(
    discussion_id: int,
    discussion_msg_id: int,
    channel_post_id: int,
) -> None:
    """★ 新增：添加讨论组消息到频道帖子的映射"""
    key = (discussion_id, discussion_msg_id)
    discussion_to_channel_post[key] = channel_post_id
    logging.debug(
        f"📎 讨论组映射: ({discussion_id}, {discussion_msg_id}) → post {channel_post_id}"
    )


def get_channel_post_id(
    discussion_id: int,
    discussion_msg_id: int,
) -> Optional[int]:
    """★ 新增：获取讨论组消息对应的频道帖子 ID"""
    key = (discussion_id, discussion_msg_id)
    return discussion_to_channel_post.get(key)


# ========== 媒体组缓存 ==========

GROUPED_CACHE: Dict[int, Dict[int, List[Message]]] = {}
GROUPED_TIMERS: Dict[int, asyncio.TimerHandle] = {}
GROUPED_TIMEOUT = 1.5
GROUPED_MAPPING: Dict[int, Dict[int, List[int]]] = {}


async def _flush_group(grouped_id: int) -> None:
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
    for grouped_id, mapping in GROUPED_MAPPING.items():
        if chat_id in mapping and msg_id in mapping[chat_id]:
            return mapping[chat_id]
    return None


def debug_post_mappings() -> str:
    """★ 调试用：打印当前所有帖子映射"""
    lines = ["=== 帖子映射状态 ==="]
    for (src_ch, src_post), dest_map in post_id_mapping.items():
        for dest_ch, dest_post in dest_map.items():
            lines.append(f"  ({src_ch}, {src_post}) → ({dest_ch}, {dest_post})")
    lines.append(f"总计: {len(post_id_mapping)} 个源帖子映射")
    return "\n".join(lines)
