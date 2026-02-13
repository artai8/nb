import asyncio
import logging
import os
import platform
import random
import re
import sys
import time
from datetime import datetime
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple, Union

from telethon.client import TelegramClient
from telethon.hints import EntityLike
from telethon.tl.custom.message import Message
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.functions.messages import GetDiscussionMessageRequest, SendMediaRequest, SendMultiMediaRequest
from telethon.tl.types import (
    InputDocument,
    InputMediaDocument,
    InputMediaPhoto,
    InputPhoto,
    InputSingleMedia,
    MessageMediaDocument,
    MessageMediaPhoto,
)

import nb.storage as st
from nb import __version__
from nb.config import CONFIG
from nb.plugin_models import STYLE_CODES

if TYPE_CHECKING:
    from nb.plugins import NbMessage

MAX_RETRIES = 3
COMMENT_MAX_RETRIES = 6
COMMENT_RETRY_BASE_DELAY = 3
DISCUSSION_CACHE: Dict[int, int] = {}


def extract_msg_id(fwded) -> Optional[int]:
    if fwded is None:
        return None
    if isinstance(fwded, int):
        return fwded
    if isinstance(fwded, list):
        return fwded[0].id if fwded and hasattr(fwded[0], "id") else None
    return getattr(fwded, "id", None)


def _get_reply_to_msg_id(message) -> Optional[int]:
    if hasattr(message, "reply_to_msg_id") and message.reply_to_msg_id is not None:
        return message.reply_to_msg_id
    if hasattr(message, "reply_to") and message.reply_to is not None:
        return getattr(message.reply_to, "reply_to_msg_id", None)
    return None


def _get_reply_to_top_id(message) -> Optional[int]:
    reply_to = getattr(message, "reply_to", None)
    if reply_to is None:
        return None
    return getattr(reply_to, "reply_to_top_id", None)


def _extract_channel_post(msg) -> Optional[int]:
    if msg and hasattr(msg, "fwd_from") and msg.fwd_from:
        return getattr(msg.fwd_from, "channel_post", None)
    return None


async def walk_to_header(client, chat_id, msg_id, max_depth=20) -> Tuple[Optional[int], Optional[int]]:
    current_id = msg_id
    visited = set()
    for _ in range(max_depth):
        if current_id is None or current_id in visited:
            break
        visited.add(current_id)
        cp = st.discussion_to_channel_post.get((chat_id, current_id))
        if cp:
            return current_id, cp
        try:
            msg = await client.get_messages(chat_id, ids=current_id)
            if not msg:
                break
            cp = _extract_channel_post(msg)
            if cp:
                st.add_discussion_mapping(chat_id, current_id, cp)
                return current_id, cp
            rt = getattr(msg, "reply_to", None)
            if rt:
                top = getattr(rt, "reply_to_top_id", None)
                if top and top not in visited:
                    current_id = top
                    continue
                parent = getattr(rt, "reply_to_msg_id", None)
                if parent and parent not in visited:
                    current_id = parent
                    continue
            break
        except Exception:
            break
    try:
        async for msg in client.iter_messages(chat_id, limit=50, max_id=(msg_id or 0) + 1):
            cp = _extract_channel_post(msg)
            if cp:
                st.add_discussion_mapping(chat_id, msg.id, cp)
                return msg.id, cp
    except Exception:
        pass
    return None, None


async def get_discussion_message(client, channel_id, msg_id) -> Optional[Message]:
    for attempt in range(COMMENT_MAX_RETRIES):
        try:
            result = await client(GetDiscussionMessageRequest(peer=channel_id, msg_id=msg_id))
            if result and result.messages:
                return result.messages[0]
            return None
        except Exception as e:
            err_str = str(e).upper()
            if "FLOOD" in err_str:
                wait_match = re.search(r"\d+", str(e))
                wait = int(wait_match.group()) + 5 if wait_match else min(15 * (attempt + 1), 120)
                logging.warning(f"获取讨论消息FloodWait, 等待{wait}秒")
                await asyncio.sleep(wait)
                continue
            if any(
                k in err_str
                for k in ("MSG_ID_INVALID", "CHANNEL_PRIVATE", "CHAT_ADMIN_REQUIRED", "PEER_ID_INVALID", "DISCUSSION")
            ):
                return None
            if attempt < COMMENT_MAX_RETRIES - 1:
                await asyncio.sleep(COMMENT_RETRY_BASE_DELAY * (attempt + 1))
            else:
                logging.error(f"获取讨论消息最终失败 channel={channel_id} msg={msg_id}: {e}")
    return None


async def get_discussion_group_id(client, channel_id) -> Optional[int]:
    if channel_id in DISCUSSION_CACHE:
        return DISCUSSION_CACHE[channel_id]
    for attempt in range(COMMENT_MAX_RETRIES):
        try:
            input_channel = await client.get_input_entity(channel_id)
            full_result = await client(GetFullChannelRequest(input_channel))
            linked = getattr(full_result.full_chat, "linked_chat_id", None)
            if linked:
                DISCUSSION_CACHE[channel_id] = linked
            return linked
        except Exception as e:
            err_str = str(e).upper()
            if "FLOOD" in err_str:
                wait_match = re.search(r"\d+", str(e))
                wait = int(wait_match.group()) + 5 if wait_match else 15 * (attempt + 1)
                await asyncio.sleep(wait)
                continue
            if any(k in err_str for k in ("CHANNEL_PRIVATE", "CHAT_ADMIN_REQUIRED")):
                return None
            if attempt < COMMENT_MAX_RETRIES - 1:
                await asyncio.sleep(COMMENT_RETRY_BASE_DELAY * (attempt + 1))
    return None


async def preload_discussion_mappings(client, discussion_id, limit=1000):
    count = 0
    try:
        async for msg in client.iter_messages(discussion_id, limit=limit):
            cp = _extract_channel_post(msg)
            if cp:
                st.add_discussion_mapping(discussion_id, msg.id, cp)
                count += 1
    except Exception as e:
        logging.warning(f"预加载帖子映射失败 (讨论组={discussion_id}): {e}")
    return count


def _has_spoiler(message) -> bool:
    if not message or not message.media:
        return False
    return getattr(message.media, "spoiler", False)


async def _send_single_with_spoiler(client, recipient, message, caption=None, reply_to=None):
    media = message.media
    peer = await client.get_input_entity(recipient)
    if isinstance(media, MessageMediaPhoto) and media.photo:
        p = media.photo
        input_media = InputMediaPhoto(
            id=InputPhoto(id=p.id, access_hash=p.access_hash, file_reference=p.file_reference), spoiler=True
        )
    elif isinstance(media, MessageMediaDocument) and media.document:
        d = media.document
        input_media = InputMediaDocument(
            id=InputDocument(id=d.id, access_hash=d.access_hash, file_reference=d.file_reference), spoiler=True
        )
    else:
        raise ValueError(f"不支持的媒体类型: {type(media)}")
    result = await client(
        SendMediaRequest(peer=peer, media=input_media, message=caption or "", random_id=random.randrange(-2**63, 2**63), reply_to_msg_id=reply_to)
    )
    if hasattr(result, "updates"):
        for update in result.updates:
            if hasattr(update, "message"):
                return update.message
    return result


async def _send_album_with_spoiler(client, recipient, grouped_messages, caption=None, reply_to=None):
    peer = await client.get_input_entity(recipient)
    multi_media = []
    for i, msg in enumerate(grouped_messages):
        media = msg.media
        is_spoiler = _has_spoiler(msg)
        msg_text = caption if (i == 0 and caption) else ""
        input_media = None
        if isinstance(media, MessageMediaPhoto) and media.photo:
            p = media.photo
            input_media = InputMediaPhoto(
                id=InputPhoto(id=p.id, access_hash=p.access_hash, file_reference=p.file_reference), spoiler=is_spoiler
            )
        elif isinstance(media, MessageMediaDocument) and media.document:
            d = media.document
            input_media = InputMediaDocument(
                id=InputDocument(id=d.id, access_hash=d.access_hash, file_reference=d.file_reference), spoiler=is_spoiler
            )
        if input_media is None:
            continue
        multi_media.append(
            InputSingleMedia(media=input_media, random_id=random.randrange(-2**63, 2**63), message=msg_text)
        )
    if not multi_media:
        raise ValueError("没有有效的媒体")
    kwargs = {"peer": peer, "multi_media": multi_media}
    if reply_to is not None:
        kwargs["reply_to_msg_id"] = reply_to
    result = await client(SendMultiMediaRequest(**kwargs))
    sent = []
    if hasattr(result, "updates"):
        for update in result.updates:
            if hasattr(update, "message"):
                sent.append(update.message)
    return sent if sent else result


def platform_info():
    nl = "\n"
    return f"Running nb {__version__}\nPython {sys.version.replace(nl,'')}\nOS {os.name}\nPlatform {platform.system()} {platform.release()}\n{platform.architecture()} {platform.processor()}"


async def send_message(recipient, tm, grouped_messages=None, grouped_tms=None, comment_to_post=None):
    client = tm.client
    effective_reply_to = comment_to_post if comment_to_post else tm.reply_to
    if CONFIG.show_forwarded_from and grouped_messages:
        for attempt in range(MAX_RETRIES):
            try:
                return await client.forward_messages(recipient, grouped_messages)
            except Exception as e:
                if "FLOOD_WAIT" in str(e).upper():
                    wait_match = re.search(r"\d+", str(e))
                    await asyncio.sleep((int(wait_match.group()) if wait_match else 30) + 10)
                else:
                    logging.error(f"转发失败 ({attempt+1}/{MAX_RETRIES}): {e}")
                await asyncio.sleep(min(5 * 2 ** attempt, 300))
        return None
    if grouped_messages and grouped_tms:
        combined_caption = "\n\n".join([gtm.text.strip() for gtm in grouped_tms if gtm.text and gtm.text.strip()])
        any_spoiler = any(_has_spoiler(msg) for msg in grouped_messages)
        for attempt in range(MAX_RETRIES):
            try:
                if any_spoiler:
                    return await _send_album_with_spoiler(
                        client, recipient, grouped_messages, caption=combined_caption or None, reply_to=effective_reply_to
                    )
                else:
                    files = [msg for msg in grouped_messages if msg.photo or msg.video or msg.gif or msg.document]
                    if not files:
                        return await client.send_message(recipient, combined_caption or "空相册", reply_to=effective_reply_to)
                    return await client.send_file(
                        recipient,
                        files,
                        caption=combined_caption or None,
                        reply_to=effective_reply_to,
                        supports_streaming=True,
                        force_document=False,
                        allow_cache=False,
                        parse_mode="md",
                    )
            except Exception as e:
                if "FLOOD_WAIT" in str(e).upper():
                    wait_match = re.search(r"\d+", str(e))
                    await asyncio.sleep((int(wait_match.group()) if wait_match else 30) + 10)
                else:
                    logging.error(f"媒体组发送失败 ({attempt+1}/{MAX_RETRIES}): {e}")
                await asyncio.sleep(min(5 * 2 ** attempt, 300))
        return None
    processed_markup = getattr(tm, "reply_markup", None)
    if tm.new_file:
        try:
            return await client.send_file(
                recipient, tm.new_file, caption=tm.text, reply_to=effective_reply_to, supports_streaming=True, buttons=processed_markup
            )
        except Exception:
            try:
                return await client.send_file(recipient, tm.new_file, caption=tm.text, reply_to=effective_reply_to, supports_streaming=True)
            except Exception as e:
                logging.error(f"文件发送失败: {e}")
                return None
    if _has_spoiler(tm.message):
        try:
            return await _send_single_with_spoiler(client, recipient, tm.message, caption=tm.text, reply_to=effective_reply_to)
        except Exception:
            pass
    try:
        if processed_markup is not None:
            try:
                return await client.send_message(
                    recipient,
                    tm.text,
                    file=tm.message.media if tm.message.media else None,
                    buttons=processed_markup,
                    reply_to=effective_reply_to,
                    link_preview=not bool(tm.message.media),
                )
            except Exception:
                if tm.message.media:
                    return await client.send_message(
                        recipient, tm.text, file=tm.message.media, reply_to=effective_reply_to, link_preview=False
                    )
                return await client.send_message(recipient, tm.text, reply_to=effective_reply_to)
        else:
            if tm.message.media:
                return await client.send_message(
                    recipient, tm.text, file=tm.message.media, reply_to=effective_reply_to, link_preview=False
                )
            return await client.send_message(recipient, tm.text, reply_to=effective_reply_to)
    except Exception as e:
        logging.error(f"消息发送失败: {e}")
        return None


def cleanup(*files):
    for f in files:
        try:
            os.remove(f)
        except FileNotFoundError:
            pass


def stamp(file, user):
    outf = safe_name(f"{user} {datetime.now()} {file}")
    try:
        os.rename(file, outf)
        return outf
    except Exception:
        return file


def safe_name(string):
    return re.sub(r"[-!@#$%^&*()\s]", "_", string)


def match(pattern, string, regex):
    if regex:
        return bool(re.findall(pattern, string))
    return pattern in string


def replace(pattern, new, string, regex):
    def fmt_repl(matched):
        code = STYLE_CODES.get(new)
        return f"{code}{matched.group(0)}{code}" if code else new

    if regex:
        if new in STYLE_CODES:
            return re.compile(pattern).sub(repl=fmt_repl, string=string)
        return re.sub(pattern, new, string)
    if new in STYLE_CODES:
        code = STYLE_CODES[new]
        return string.replace(pattern, f"{code}{pattern}{code}")
    return string.replace(pattern, new)


def clean_session_files():
    for item in os.listdir():
        if item.endswith(".session") or item.endswith(".session-journal"):
            os.remove(item)


async def wait_for_dest_post_id(src_channel_id: int, src_post_id: int, dest_channel_id: int, timeout: int = 60) -> Optional[int]:
    """等待直到目标帖子ID映射建立，最多等待timeout秒"""
    start = time.time()
    while time.time() - start < timeout:
        dest_post_id = st.get_dest_post_id(src_channel_id, src_post_id, dest_channel_id)
        if dest_post_id is not None:
            return dest_post_id
        await asyncio.sleep(1)
    return None
