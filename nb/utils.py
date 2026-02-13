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

from telethon.tl.custom.message import Message
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.functions.messages import GetDiscussionMessageRequest

import nb.storage as st
from nb import __version__
from nb.config import CONFIG
from nb.plugin_models import STYLE_CODES

if TYPE_CHECKING:
    from nb.plugins import NbMessage

MAX_RETRIES = 3
COMMENT_MAX_RETRIES = 5
COMMENT_RETRY_BASE_DELAY = 2
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


async def get_discussion_message(client, channel_id: int, msg_id: int) -> Optional[Message]:
    for attempt in range(COMMENT_MAX_RETRIES):
        try:
            result = await client(GetDiscussionMessageRequest(peer=channel_id, msg_id=msg_id))
            if result and result.messages:
                msg = result.messages[0]
                st.add_discussion_mapping(msg.chat_id, msg.id, msg_id)
                return msg
            return None
        except Exception as e:
            err_str = str(e).upper()
            if "FLOOD" in err_str:
                wait_match = re.search(r"\d+", str(e))
                wait = int(wait_match.group()) + 5 if wait_match else 30
                logging.warning(f"FloodWait {wait}秒")
                await asyncio.sleep(wait)
                continue
            if any(k in err_str for k in ("MSG_ID_INVALID", "CHANNEL_PRIVATE", "PEER_ID_INVALID")):
                return None
            if attempt < COMMENT_MAX_RETRIES - 1:
                await asyncio.sleep(COMMENT_RETRY_BASE_DELAY * (attempt + 1))
            else:
                logging.warning(f"获取讨论消息失败: {channel_id}/{msg_id}: {e}")
    return None


async def get_discussion_group_id(client, channel_id: int) -> Optional[int]:
    if channel_id in DISCUSSION_CACHE:
        return DISCUSSION_CACHE[channel_id]
    for attempt in range(COMMENT_MAX_RETRIES):
        try:
            input_channel = await client.get_input_entity(channel_id)
            full_result = await client(GetFullChannelRequest(input_channel))
            linked = getattr(full_result.full_chat, "linked_chat_id", None)
            if linked:
                DISCUSSION_CACHE[channel_id] = linked
                logging.info(f"讨论组: 频道={channel_id} -> 讨论组={linked}")
            return linked
        except Exception as e:
            err_str = str(e).upper()
            if "FLOOD" in err_str:
                wait_match = re.search(r"\d+", str(e))
                wait = int(wait_match.group()) + 5 if wait_match else 30
                await asyncio.sleep(wait)
                continue
            if any(k in err_str for k in ("CHANNEL_PRIVATE", "CHAT_ADMIN_REQUIRED")):
                return None
            if attempt < COMMENT_MAX_RETRIES - 1:
                await asyncio.sleep(COMMENT_RETRY_BASE_DELAY * (attempt + 1))
    return None


async def preload_discussion_mappings(client, discussion_id: int, limit: int = 500) -> int:
    count = 0
    try:
        async for msg in client.iter_messages(discussion_id, limit=limit):
            cp = _extract_channel_post(msg)
            if cp:
                st.add_discussion_mapping(discussion_id, msg.id, cp)
                count += 1
    except Exception as e:
        logging.warning(f"预加载映射失败: {discussion_id}: {e}")
    return count


def platform_info():
    nl = "\n"
    return (
        f"Running nb {__version__}\n"
        f"Python {sys.version.replace(nl,'')}\n"
        f"OS {os.name}\n"
        f"Platform {platform.system()} {platform.release()}\n"
        f"{platform.architecture()} {platform.processor()}"
    )


async def _download_media_to_bytes(client, message) -> Optional[bytes]:
    """将消息中的媒体下载为字节"""
    try:
        data = await client.download_media(message, file=bytes)
        return data
    except Exception as e:
        logging.warning(f"下载媒体失败: {e}")
        return None


async def _get_file_name(message) -> Optional[str]:
    """获取消息中媒体的文件名"""
    try:
        if hasattr(message, "file") and message.file:
            return message.file.name
    except Exception:
        pass
    return None


async def _is_video(message) -> bool:
    """判断消息是否是视频"""
    try:
        if message.video:
            return True
        if message.document:
            mime = getattr(message.document, "mime_type", "") or ""
            if mime.startswith("video/"):
                return True
    except Exception:
        pass
    return False


async def _send_single(client, recipient, message, caption=None, reply_to=None):
    """发送单条带媒体的消息 - 始终下载后重新上传"""
    if not message.media:
        # 纯文本
        if caption:
            return await client.send_message(recipient, caption, reply_to=reply_to)
        return None

    # 下载媒体
    media_data = await _download_media_to_bytes(client, message)
    if not media_data:
        # 下载失败，只发文本
        if caption:
            return await client.send_message(recipient, caption, reply_to=reply_to)
        return None

    file_name = await _get_file_name(message)
    is_vid = await _is_video(message)

    try:
        return await client.send_file(
            recipient,
            media_data,
            caption=caption,
            reply_to=reply_to,
            file_name=file_name,
            supports_streaming=is_vid,
            force_document=False,
        )
    except Exception as e:
        logging.error(f"上传媒体失败: {e}")
        # 降级：只发文本
        if caption:
            try:
                return await client.send_message(recipient, caption, reply_to=reply_to)
            except Exception:
                pass
        return None


async def _send_album(client, recipient, messages, caption=None, reply_to=None):
    """发送媒体组 - 始终下载后重新上传"""
    files = []
    for msg in messages:
        if not msg.media:
            continue
        data = await _download_media_to_bytes(client, msg)
        if data:
            files.append(data)

    if not files:
        if caption:
            return await client.send_message(recipient, caption, reply_to=reply_to)
        return None

    try:
        return await client.send_file(
            recipient,
            files,
            caption=caption,
            reply_to=reply_to,
            supports_streaming=True,
            force_document=False,
        )
    except Exception as e:
        logging.error(f"上传媒体组失败: {e}")
        # 降级：逐条发送
        sent = None
        for i, f in enumerate(files):
            try:
                cap = caption if i == 0 else None
                sent = await client.send_file(
                    recipient,
                    f,
                    caption=cap,
                    reply_to=reply_to,
                    supports_streaming=True,
                    force_document=False,
                )
            except Exception as e2:
                logging.error(f"逐条发送也失败: {e2}")
        # 如果全失败，只发文本
        if sent is None and caption:
            try:
                return await client.send_message(recipient, caption, reply_to=reply_to)
            except Exception:
                pass
        return sent


async def send_message(recipient, tm, grouped_messages=None, grouped_tms=None, comment_to_post=None):
    """发送消息主函数"""
    client = tm.client
    effective_reply_to = comment_to_post if comment_to_post else tm.reply_to

    # ========== 媒体组 ==========
    if grouped_messages and grouped_tms:
        combined_caption = "\n\n".join(
            [gtm.text.strip() for gtm in grouped_tms if gtm.text and gtm.text.strip()]
        )

        for attempt in range(MAX_RETRIES):
            try:
                result = await _send_album(
                    client,
                    recipient,
                    grouped_messages,
                    caption=combined_caption or None,
                    reply_to=effective_reply_to,
                )
                if result:
                    return result
            except Exception as e:
                err_str = str(e).upper()
                if "FLOOD" in err_str:
                    wait_match = re.search(r"\d+", str(e))
                    wait_time = int(wait_match.group()) + 10 if wait_match else 60
                    logging.warning(f"FloodWait {wait_time}秒")
                    await asyncio.sleep(wait_time)
                else:
                    logging.error(f"媒体组 ({attempt+1}/{MAX_RETRIES}): {e}")
                    await asyncio.sleep(5 * (attempt + 1))
        return None

    # ========== 单条消息 ==========
    text = tm.text or ""

    # 有处理过的新文件
    if tm.new_file:
        for attempt in range(MAX_RETRIES):
            try:
                return await client.send_file(
                    recipient,
                    tm.new_file,
                    caption=text,
                    reply_to=effective_reply_to,
                    supports_streaming=True,
                )
            except Exception as e:
                err_str = str(e).upper()
                if "FLOOD" in err_str:
                    wait_match = re.search(r"\d+", str(e))
                    await asyncio.sleep((int(wait_match.group()) if wait_match else 30) + 10)
                else:
                    logging.error(f"new_file ({attempt+1}/{MAX_RETRIES}): {e}")
                    await asyncio.sleep(5 * (attempt + 1))
        # 降级
        if text:
            try:
                return await client.send_message(recipient, text, reply_to=effective_reply_to)
            except Exception:
                pass
        return None

    # 有媒体
    if tm.message and tm.message.media:
        for attempt in range(MAX_RETRIES):
            try:
                result = await _send_single(
                    client,
                    recipient,
                    tm.message,
                    caption=text,
                    reply_to=effective_reply_to,
                )
                if result:
                    return result
            except Exception as e:
                err_str = str(e).upper()
                if "FLOOD" in err_str:
                    wait_match = re.search(r"\d+", str(e))
                    wait_time = int(wait_match.group()) + 10 if wait_match else 60
                    logging.warning(f"FloodWait {wait_time}秒")
                    await asyncio.sleep(wait_time)
                else:
                    logging.error(f"单条媒体 ({attempt+1}/{MAX_RETRIES}): {e}")
                    await asyncio.sleep(5 * (attempt + 1))
        # 降级
        if text:
            try:
                return await client.send_message(recipient, text, reply_to=effective_reply_to)
            except Exception:
                pass
        return None

    # 纯文本
    if text:
        try:
            return await client.send_message(recipient, text, reply_to=effective_reply_to)
        except Exception as e:
            logging.error(f"发送文本失败: {e}")

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
