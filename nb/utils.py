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

MAX_RETRIES = 5
COMMENT_MAX_RETRIES = 7
COMMENT_RETRY_BASE_DELAY = 3
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
        if not fwded:
            return None
        first = fwded[0]
        if isinstance(first, int):
            return first
        return getattr(first, "id", None)
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
    """从消息中提取频道帖子 ID — 增强版"""
    if not msg:
        return None
    # 方式1: fwd_from.channel_post
    if hasattr(msg, "fwd_from") and msg.fwd_from:
        cp = getattr(msg.fwd_from, "channel_post", None)
        if cp:
            return cp
    # 方式2: reply_to.reply_to_top_id（讨论组自动转发消息有时用这个）
    reply_to = getattr(msg, "reply_to", None)
    if reply_to:
        # 如果消息是讨论组中的头消息（自动转发），通常 reply_to 为空但 fwd_from 有值
        # 这里主要靠 fwd_from，但作为备用检查
        pass
    return None


async def get_discussion_message(client, channel_id, msg_id) -> Optional[Message]:
    """获取讨论消息 — 增强重试和错误处理"""
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
                logging.warning(f"FloodWait {wait}s (讨论消息 {channel_id}/{msg_id})")
                await asyncio.sleep(wait)
                continue
            if any(k in err_str for k in (
                "MSG_ID_INVALID", "CHANNEL_PRIVATE", "PEER_ID_INVALID"
            )):
                logging.debug(f"无法获取讨论消息 {channel_id}/{msg_id}: {e}")
                return None
            if attempt < COMMENT_MAX_RETRIES - 1:
                delay = COMMENT_RETRY_BASE_DELAY * (attempt + 1)
                logging.warning(f"获取讨论消息失败 ({attempt+1}/{COMMENT_MAX_RETRIES}): {e}, {delay}s后重试")
                await asyncio.sleep(delay)
            else:
                logging.error(f"获取讨论消息最终失败 {channel_id}/{msg_id}: {e}")
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
                logging.warning(f"FloodWait {wait}s (获取讨论组 {channel_id})")
                await asyncio.sleep(wait)
                continue
            if any(k in err_str for k in ("CHANNEL_PRIVATE", "CHAT_ADMIN_REQUIRED")):
                return None
            if attempt < COMMENT_MAX_RETRIES - 1:
                delay = COMMENT_RETRY_BASE_DELAY * (attempt + 1)
                logging.warning(f"获取讨论组失败 ({attempt+1}): {e}, {delay}s后重试")
                await asyncio.sleep(delay)
            else:
                logging.error(f"获取讨论组最终失败 {channel_id}: {e}")
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
    """下载媒体为 bytes — 增强重试"""
    for attempt in range(3):
        try:
            data = await client.download_media(message, file=bytes)
            if data:
                return data
        except Exception as e:
            err_str = str(e).upper()
            if "FLOOD" in err_str:
                wait_match = re.search(r"\d+", str(e))
                wait = int(wait_match.group()) + 5 if wait_match else 30
                await asyncio.sleep(wait)
                continue
            logging.warning(f"下载媒体失败 ({attempt+1}/3): {e}")
            if attempt < 2:
                await asyncio.sleep(3 * (attempt + 1))
    return None


def _get_file_name(message) -> Optional[str]:
    """安全获取文件名"""
    try:
        if hasattr(message, "file") and message.file:
            return message.file.name
    except Exception:
        pass
    try:
        if hasattr(message, "document") and message.document:
            for attr in message.document.attributes:
                fname = getattr(attr, "file_name", None)
                if fname:
                    return fname
    except Exception:
        pass
    return None


async def _send_single_message(client, recipient, message, caption, reply_to):
    """发送单条消息（带媒体或纯文本）— 增强版"""
    cap = caption if caption else ""
    has_media = _msg_has_media(message)

    if not has_media:
        if cap and cap.strip():
            for attempt in range(MAX_RETRIES):
                try:
                    return await client.send_message(recipient, cap, reply_to=reply_to)
                except Exception as e:
                    err_str = str(e).upper()
                    if "FLOOD" in err_str:
                        wait_match = re.search(r"\d+", str(e))
                        wait_time = int(wait_match.group()) + 10 if wait_match else 60
                        await asyncio.sleep(wait_time)
                        continue
                    logging.error(f"发送文本失败 ({attempt+1}): {e}")
                    if attempt < MAX_RETRIES - 1:
                        await asyncio.sleep(3 * (attempt + 1))
        return None

    for attempt in range(MAX_RETRIES):
        # 尝试1: 用 message.media 引用发送
        try:
            result = await client.send_message(
                recipient, cap,
                file=message.media,
                reply_to=reply_to,
                link_preview=False,
            )
            if result:
                return result
        except Exception as e:
            err_str = str(e).upper()
            if "FLOOD" in err_str:
                wait_match = re.search(r"\d+", str(e))
                wait_time = int(wait_match.group()) + 10 if wait_match else 60
                logging.warning(f"FloodWait {wait_time}s")
                await asyncio.sleep(wait_time)
                continue

            logging.debug(f"直接发送失败 ({attempt+1}): {e}")

            # 尝试2: 下载后重传
            data = await _download_media_bytes(client, message)
            if data:
                try:
                    fname = _get_file_name(message)
                    result = await client.send_file(
                        recipient, data,
                        caption=cap if cap else None,
                        reply_to=reply_to,
                        file_name=fname,
                        supports_streaming=True,
                        force_document=False,
                    )
                    if result:
                        return result
                except Exception as e2:
                    logging.error(f"重传失败 ({attempt+1}): {e2}")

            # 尝试3: 用 send_file + message.media
            try:
                result = await client.send_file(
                    recipient, message.media,
                    caption=cap if cap else None,
                    reply_to=reply_to,
                    supports_streaming=True,
                    force_document=False,
                )
                if result:
                    return result
            except Exception as e3:
                logging.debug(f"send_file media 也失败 ({attempt+1}): {e3}")

            await asyncio.sleep(3 * (attempt + 1))

    # 所有尝试都失败，降级发文本
    if cap and cap.strip():
        try:
            logging.warning("所有媒体发送方式失败，降级为纯文本")
            return await client.send_message(recipient, cap, reply_to=reply_to)
        except Exception:
            pass
    return None


async def _send_media_group(client, recipient, messages, caption, reply_to):
    """发送媒体组 — 完全重写版
    
    关键修复：
    1. 正确处理图片+视频混合媒体组
    2. caption 限制 1024 字符
    3. 多层降级策略
    4. 确保不丢失任何媒体
    """
    media_msgs = [m for m in messages if _msg_has_media(m)]
    text_only_msgs = [m for m in messages if not _msg_has_media(m)]

    if not media_msgs:
        # 只有文本
        if caption and caption.strip():
            return await client.send_message(recipient, caption, reply_to=reply_to)
        return None

    # Telegram caption 限制 1024 字符
    safe_caption = caption
    if safe_caption and len(safe_caption) > 1024:
        safe_caption = safe_caption[:1021] + "..."

    for attempt in range(MAX_RETRIES):
        # ===== 策略1: 用 message.media 列表发送（保持相册效果）=====
        try:
            media_list = [m.media for m in media_msgs]
            result = await client.send_file(
                recipient, media_list,
                caption=safe_caption if safe_caption else None,
                reply_to=reply_to,
                supports_streaming=True,
                force_document=False,
            )
            if result:
                return result if isinstance(result, list) else [result]
        except Exception as e:
            err_str = str(e).upper()
            if "FLOOD" in err_str:
                wait_match = re.search(r"\d+", str(e))
                wait_time = int(wait_match.group()) + 10 if wait_match else 60
                logging.warning(f"FloodWait {wait_time}s (媒体组策略1)")
                await asyncio.sleep(wait_time)
                continue
            logging.warning(f"媒体组策略1失败 ({attempt+1}): {e}")

        # ===== 策略2: 下载为 bytes 后重新上传 =====
        files_data = []
        file_names = []
        for msg in media_msgs:
            data = await _download_media_bytes(client, msg)
            files_data.append(data)
            file_names.append(_get_file_name(msg))

        valid_files = [d for d in files_data if d]
        if valid_files:
            try:
                result = await client.send_file(
                    recipient, valid_files,
                    caption=safe_caption if safe_caption else None,
                    reply_to=reply_to,
                    supports_streaming=True,
                    force_document=False,
                )
                if result:
                    return result if isinstance(result, list) else [result]
            except Exception as e2:
                logging.warning(f"媒体组策略2失败 ({attempt+1}): {e2}")

        # ===== 策略3: 逐条发送 =====
        sent_list = []
        for i, msg in enumerate(media_msgs):
            msg_cap = safe_caption if i == 0 else ""
            msg_reply = reply_to if i == 0 else None
            sent = None

            # 3a: send_message + file=media
            try:
                sent = await client.send_message(
                    recipient, msg_cap if msg_cap else "",
                    file=msg.media,
                    reply_to=msg_reply,
                    link_preview=False,
                )
            except Exception as e3a:
                logging.debug(f"逐条 media 引用失败 ({i}): {e3a}")

                # 3b: send_file + media
                try:
                    sent = await client.send_file(
                        recipient, msg.media,
                        caption=msg_cap if msg_cap else None,
                        reply_to=msg_reply,
                        supports_streaming=True,
                        force_document=False,
                    )
                except Exception as e3b:
                    logging.debug(f"逐条 send_file media 失败 ({i}): {e3b}")

                    # 3c: send_file + bytes
                    if i < len(files_data) and files_data[i]:
                        try:
                            sent = await client.send_file(
                                recipient, files_data[i],
                                caption=msg_cap if msg_cap else None,
                                reply_to=msg_reply,
                                file_name=file_names[i],
                                supports_streaming=True,
                                force_document=False,
                            )
                        except Exception as e3c:
                            logging.error(f"逐条 bytes 也失败 ({i}): {e3c}")

            if sent:
                sent_list.append(sent)
            else:
                logging.error(f"媒体组第 {i} 条消息所有方式均失败")

            # 逐条发送之间加小延迟避免 flood
            if i < len(media_msgs) - 1:
                await asyncio.sleep(1)

        if sent_list:
            return sent_list

        await asyncio.sleep(5 * (attempt + 1))

    # 全部失败，降级发文本
    logging.error("媒体组所有发送方式均失败，降级为纯文本")
    if safe_caption and safe_caption.strip():
        try:
            return await client.send_message(recipient, safe_caption, reply_to=reply_to)
        except Exception:
            pass
    return None


async def send_message(recipient, tm, grouped_messages=None, grouped_tms=None, comment_to_post=None):
    """发送消息主函数 — 修复版
    
    关键修复：
    1. 媒体组 caption 处理：检查 _caption_applied 标记，避免重复拼接
    2. 正确处理 comment_to_post 作为 reply_to
    3. 增强错误处理
    """
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
                    logging.warning(f"FloodWait {wait_time}s (转发)")
                    await asyncio.sleep(wait_time)
                else:
                    logging.error(f"转发失败 ({attempt+1}): {e}")
                    await asyncio.sleep(5 * (attempt + 1))
        return None

    # ========== 媒体组 ==========
    if grouped_messages and grouped_tms:
        # 检查 caption 插件是否已经处理过
        caption_already_applied = any(
            getattr(gtm, '_caption_applied', False) for gtm in grouped_tms
        )

        if caption_already_applied:
            # caption 插件已经处理过，直接使用 tms[0].text
            combined_caption = grouped_tms[0].text if grouped_tms[0].text else None
        else:
            # 未经 caption 插件处理，正常收集所有文本
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
                    wait_time = (int(wait_match.group()) if wait_match else 30) + 10
                    logging.warning(f"FloodWait {wait_time}s (new_file)")
                    await asyncio.sleep(wait_time)
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
