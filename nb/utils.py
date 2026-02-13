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

from nb import __version__
from nb.plugin_models import STYLE_CODES

if TYPE_CHECKING:
    from nb.plugins import NbMessage

MAX_RETRIES = 3
COMMENT_MAX_RETRIES = 5
COMMENT_RETRY_BASE_DELAY = 2
DISCUSSION_CACHE: Dict[int, int] = {}


def _get_config():
    from nb.config import CONFIG
    return CONFIG


def _get_storage():
    from nb import storage as st
    return st


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


async def get_discussion_message(client, channel_id, msg_id) -> Optional[Message]:
    st = _get_storage()
    for attempt in range(COMMENT_MAX_RETRIES):
        try:
            result = await client(GetDiscussionMessageRequest(
                peer=channel_id, msg_id=msg_id
            ))
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
                await asyncio.sleep(wait)
                continue
            if any(k in err_str for k in (
                "MSG_ID_INVALID", "CHANNEL_PRIVATE", "PEER_ID_INVALID"
            )):
                return None
            if attempt < COMMENT_MAX_RETRIES - 1:
                await asyncio.sleep(COMMENT_RETRY_BASE_DELAY * (attempt + 1))
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
                wait = int(wait_match.group()) + 5 if wait_match else 30
                await asyncio.sleep(wait)
                continue
            if any(k in err_str for k in ("CHANNEL_PRIVATE", "CHAT_ADMIN_REQUIRED")):
                return None
            if attempt < COMMENT_MAX_RETRIES - 1:
                await asyncio.sleep(COMMENT_RETRY_BASE_DELAY * (attempt + 1))
    return None


async def preload_discussion_mappings(client, discussion_id, limit=500):
    st = _get_storage()
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
        f"Python {sys.version.replace(nl, '')}\n"
        f"OS {os.name}\n"
        f"Platform {platform.system()} {platform.release()}\n"
        f"{platform.architecture()} {platform.processor()}"
    )


def _msg_has_media(message) -> bool:
    if not message:
        return False
    return bool(message.media)


async def _download_media_bytes(client, message) -> Optional[bytes]:
    try:
        return await client.download_media(message, file=bytes)
    except Exception as e:
        logging.warning(f"下载媒体失败: {e}")
        return None


async def _send_single_message(client, recipient, message, caption, reply_to):
    """发送单条消息（带媒体或纯文本）"""
    cap = caption if caption else ""
    has_media = _msg_has_media(message)

    if not has_media:
        if cap:
            return await client.send_message(recipient, cap, reply_to=reply_to)
        return None

    for attempt in range(MAX_RETRIES):
        # 尝试1: 用 message.media 引用发送
        try:
            return await client.send_message(
                recipient, cap,
                file=message.media,
                reply_to=reply_to,
                link_preview=False,
            )
        except Exception as e:
            err_str = str(e).upper()
            if "FLOOD" in err_str:
                wait_match = re.search(r"\d+", str(e))
                wait_time = int(wait_match.group()) + 10 if wait_match else 60
                await asyncio.sleep(wait_time)
                continue

            logging.debug(f"直接发送失败({attempt+1}): {e}")

            # 尝试2: 下载后重传
            data = await _download_media_bytes(client, message)
            if data:
                try:
                    fname = None
                    if hasattr(message, "file") and message.file:
                        fname = message.file.name
                    return await client.send_file(
                        recipient, data,
                        caption=cap if cap else None,
                        reply_to=reply_to,
                        file_name=fname,
                        supports_streaming=True,
                        force_document=False,
                    )
                except Exception as e2:
                    logging.error(f"重传失败({attempt+1}): {e2}")

            await asyncio.sleep(3 * (attempt + 1))

    if cap and cap.strip():
        try:
            return await client.send_message(recipient, cap, reply_to=reply_to)
        except Exception:
            pass
    return None


async def _send_media_group(client, recipient, messages, caption, reply_to):
    """发送媒体组 — 修复版"""
    media_msgs = [m for m in messages if _msg_has_media(m)]
    if not media_msgs:
        if caption and caption.strip():
            return await client.send_message(recipient, caption, reply_to=reply_to)
        return None

    # Telegram caption 限制 1024 字符
    if caption and len(caption) > 1024:
        caption = caption[:1021] + "..."

    for attempt in range(MAX_RETRIES):
        # ===== 尝试1: 用 message.media 列表发送（保持相册效果）=====
        try:
            media_list = [m.media for m in media_msgs]
            result = await client.send_file(
                recipient, media_list,
                caption=caption if caption else None,
                reply_to=reply_to,
                supports_streaming=True,
                force_document=False,
            )
            if result:
                return result
        except Exception as e:
            err_str = str(e).upper()
            if "FLOOD" in err_str:
                wait_match = re.search(r"\d+", str(e))
                wait_time = int(wait_match.group()) + 10 if wait_match else 60
                await asyncio.sleep(wait_time)
                continue
            logging.warning(f"媒体组方式1失败({attempt+1}): {e}")

        # ===== 尝试2: 下载为 bytes 后重新上传（保持相册效果）=====
        files = []
        for msg in media_msgs:
            data = await _download_media_bytes(client, msg)
            if data:
                files.append(data)

        if files:
            try:
                result = await client.send_file(
                    recipient, files,
                    caption=caption if caption else None,
                    reply_to=reply_to,
                    supports_streaming=True,
                    force_document=False,
                )
                if result:
                    return result
            except Exception as e2:
                logging.warning(f"媒体组方式2失败({attempt+1}): {e2}")

        # ===== 尝试3: 逐条发送，用 send_message + file=media（与单条相同方式）=====
        sent_list = []
        for i, msg in enumerate(media_msgs):
            msg_cap = caption if i == 0 else ""
            msg_reply = reply_to if i == 0 else None
            sent = None
            # 3a: 用 message.media 引用
            try:
                sent = await client.send_message(
                    recipient, msg_cap,
                    file=msg.media,
                    reply_to=msg_reply,
                    link_preview=False,
                )
            except Exception as e3:
                logging.debug(f"逐条media引用失败: {e3}")
                # 3b: 用下载的 bytes
                if i < len(files) and files[i]:
                    try:
                        sent = await client.send_file(
                            recipient, files[i],
                            caption=msg_cap if msg_cap else None,
                            reply_to=msg_reply,
                            supports_streaming=True,
                            force_document=False,
                        )
                    except Exception as e4:
                        logging.error(f"逐条bytes也失败: {e4}")
            if sent:
                sent_list.append(sent)

        if sent_list:
            return sent_list

        await asyncio.sleep(3 * (attempt + 1))

    # 全部失败，降级发文本
    logging.error("媒体组所有发送方式均失败，降级为纯文本")
    if caption and caption.strip():
        try:
            return await client.send_message(recipient, caption, reply_to=reply_to)
        except Exception:
            pass
    return None


async def send_message(recipient, tm, grouped_messages=None, grouped_tms=None, comment_to_post=None):
    """发送消息主函数"""
    CONFIG = _get_config()
    client = tm.client
    effective_reply_to = comment_to_post if comment_to_post else tm.reply_to
    text = tm.text if tm.text else ""

    # ========== 转发模式（保留转发来源）==========
    if CONFIG.show_forwarded_from:
        msgs = grouped_messages if grouped_messages else [tm.message]
        for attempt in range(MAX_RETRIES):
            try:
                return await client.forward_messages(recipient, msgs)
            except Exception as e:
                err_str = str(e).upper()
                if "FLOOD" in err_str:
                    wait_match = re.search(r"\d+", str(e))
                    wait_time = int(wait_match.group()) + 10 if wait_match else 60
                    await asyncio.sleep(wait_time)
                else:
                    logging.error(f"转发失败 ({attempt+1}): {e}")
                    await asyncio.sleep(5 * (attempt + 1))

    # ========== 媒体组 ==========
    if grouped_messages and grouped_tms:
        all_texts = []
        for gtm in grouped_tms:
            if gtm.text and gtm.text.strip():
                all_texts.append(gtm.text.strip())
        combined_caption = "\n\n".join(all_texts) if all_texts else None

        return await _send_media_group(
            client, recipient, grouped_messages,
            caption=combined_caption,
            reply_to=effective_reply_to,
        )

    # ========== 处理过的新文件（水印等）==========
    if tm.new_file:
        for attempt in range(MAX_RETRIES):
            try:
                return await client.send_file(
                    recipient, tm.new_file,
                    caption=text if text else None,
                    reply_to=effective_reply_to,
                    supports_streaming=True,
                )
            except Exception as e:
                err_str = str(e).upper()
                if "FLOOD" in err_str:
                    wait_match = re.search(r"\d+", str(e))
                    await asyncio.sleep((int(wait_match.group()) if wait_match else 30) + 10)
                else:
                    logging.error(f"new_file ({attempt+1}): {e}")
                    await asyncio.sleep(5 * (attempt + 1))
        if text and text.strip():
            try:
                return await client.send_message(recipient, text, reply_to=effective_reply_to)
            except Exception:
                pass
        return None

    # ========== 单条消息 ==========
    return await _send_single_message(
        client, recipient, tm.message,
        caption=text,
        reply_to=effective_reply_to,
    )


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
    """清理 session 文件 — 修复版：捕获异常防止启动失败"""
    try:
        for item in os.listdir():
            if item.endswith(".session") or item.endswith(".session-journal"):
                try:
                    os.remove(item)
                except Exception as e:
                    logging.warning(f"无法删除 session 文件 {item}: {e}")
    except Exception as e:
        logging.warning(f"清理 session 文件失败: {e}")
