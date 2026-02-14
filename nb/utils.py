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
    if not msg:
        return None
    if hasattr(msg, "fwd_from") and msg.fwd_from:
        cp = getattr(msg.fwd_from, "channel_post", None)
        if cp:
            return cp
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
                logging.warning(f"FloodWait {wait}s (讨论消息 {channel_id}/{msg_id})")
                await asyncio.sleep(wait)
                continue
            if any(k in err_str for k in (
                "MSG_ID_INVALID", "CHANNEL_PRIVATE", "PEER_ID_INVALID"
            )):
                if attempt == 0:
                    logging.warning(f"讨论消息无效 {channel_id}/{msg_id}: {e}")
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
    """发送单条消息（带媒体或纯文本）"""
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

    media_type = type(message.media).__name__ if message.media else "None"
    msg_id = getattr(message, 'id', '?')
    chat_id = getattr(message, 'chat_id', '?')
    logging.info(
        f"[send_single] media_type={media_type}, msg={chat_id}/{msg_id}, "
        f"to={recipient}, caption_len={len(cap)}"
    )

    last_error = None

    for attempt in range(MAX_RETRIES):
        # 尝试1: send_message + file=message.media
        try:
            result = await client.send_message(
                recipient, cap,
                file=message.media,
                reply_to=reply_to,
                link_preview=False,
            )
            if result:
                logging.info(f"[send_single] 成功(方式1)")
                return result
        except Exception as e:
            last_error = e
            err_str = str(e).upper()
            logging.warning(f"[send_single] 方式1失败({attempt+1}): {type(e).__name__}: {e}")

            if "FLOOD" in err_str:
                wait_match = re.search(r"\d+", str(e))
                wait_time = int(wait_match.group()) + 10 if wait_match else 60
                await asyncio.sleep(wait_time)
                continue

        # 尝试2: 下载后重传
        try:
            data = await _download_media_bytes(client, message)
            if data:
                logging.info(f"[send_single] 下载成功: {len(data)} bytes")
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
                    logging.info(f"[send_single] 成功(方式2)")
                    return result
            else:
                logging.warning(f"[send_single] 下载返回空")
        except Exception as e2:
            last_error = e2
            logging.warning(f"[send_single] 方式2失败({attempt+1}): {type(e2).__name__}: {e2}")

        # 尝试3: send_file + message.media
        try:
            result = await client.send_file(
                recipient, message.media,
                caption=cap if cap else None,
                reply_to=reply_to,
                supports_streaming=True,
                force_document=False,
            )
            if result:
                logging.info(f"[send_single] 成功(方式3)")
                return result
        except Exception as e3:
            last_error = e3
            logging.warning(f"[send_single] 方式3失败({attempt+1}): {type(e3).__name__}: {e3}")

        await asyncio.sleep(3 * (attempt + 1))

    logging.error(
        f"[send_single] 全部失败! media_type={media_type}, "
        f"msg={chat_id}/{msg_id}, to={recipient}, "
        f"last_error={type(last_error).__name__}: {last_error}"
    )

    if cap and cap.strip():
        try:
            logging.warning("[send_single] 降级为纯文本")
            return await client.send_message(recipient, cap, reply_to=reply_to)
        except Exception as e_text:
            logging.error(f"[send_single] 纯文本也失败: {e_text}")
    return None


def _find_caption_text(grouped_tms):
    for gtm in grouped_tms:
        if (gtm.file_type != "nofile"
                and gtm.text
                and getattr(gtm, '_caption_applied', False)):
            return gtm.text
    for gtm in grouped_tms:
        if gtm.text and getattr(gtm, '_caption_applied', False):
            return gtm.text
    for gtm in grouped_tms:
        if gtm.text:
            return gtm.text
    return None


def _collect_caption_no_plugin(grouped_tms):
    all_texts = []
    for gtm in grouped_tms:
        content = gtm.text or gtm.raw_text or ""
        if content.strip():
            all_texts.append(content.strip())
    return "\n\n".join(all_texts) if all_texts else None


async def send_message(recipient, tm, grouped_messages=None, grouped_tms=None, comment_to_post=None):
    """发送消息主函数"""
    CONFIG = _get_config()
    client = tm.client
    effective_reply_to = comment_to_post if comment_to_post else tm.reply_to
    text = tm.text if tm.text else ""

    # ========== 转发模式 ==========
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
        return None

    # ========== 媒体组 ==========
    if grouped_messages and grouped_tms:
        if len(grouped_messages) != len(grouped_tms):
            logging.warning(
                f"[send] 数量不匹配: msgs={len(grouped_messages)} tms={len(grouped_tms)}"
            )
            grouped_messages = [t.message for t in grouped_tms]

        caption_applied = any(getattr(g, '_caption_applied', False) for g in grouped_tms)

        if caption_applied:
            combined_caption = _find_caption_text(grouped_tms)
        else:
            combined_caption = _collect_caption_no_plugin(grouped_tms)

        media_messages = [m for m in grouped_messages if _msg_has_media(m)]

        logging.info(
            f"[send] 媒体组: total={len(grouped_messages)}, "
            f"media={len(media_messages)}, "
            f"caption_applied={caption_applied}, "
            f"caption={repr(combined_caption[:50]) if combined_caption else None}, "
            f"types=[{', '.join(type(m.media).__name__ if m.media else 'None' for m in grouped_messages)}]"
        )

        if not media_messages:
            logging.warning("[send] 媒体组中没有媒体消息，降级为纯文本")
            if combined_caption and combined_caption.strip():
                try:
                    return await client.send_message(recipient, combined_caption, reply_to=effective_reply_to)
                except Exception as e:
                    logging.error(f"[send] 纯文本也失败: {e}")
            return None

        safe_caption = combined_caption
        if safe_caption and len(safe_caption) > 1024:
            safe_caption = safe_caption[:1021] + "..."

        return await _do_send_media_group(
            client, recipient, media_messages,
            caption=safe_caption,
            reply_to=effective_reply_to,
        )

    # ========== 新文件 ==========
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


async def _do_send_media_group(client, recipient, media_messages, caption, reply_to):
    """发送媒体组"""
    media_types = [type(m.media).__name__ for m in media_messages]
    logging.info(
        f"[media_group] {len(media_messages)} 个媒体, "
        f"types={media_types}, to={recipient}, "
        f"caption={repr(caption[:50]) if caption else None}"
    )

    last_error = None

    # ===== 策略1: media 列表 =====
    for attempt in range(MAX_RETRIES):
        try:
            media_list = [m.media for m in media_messages]
            result = await client.send_file(
                recipient, media_list,
                caption=caption if caption else None,
                reply_to=reply_to,
                supports_streaming=True,
                force_document=False,
            )
            if result:
                logging.info(f"[media_group] 策略1成功")
                return result if isinstance(result, list) else [result]
        except Exception as e:
            last_error = e
            err_str = str(e).upper()
            logging.warning(f"[media_group] 策略1失败({attempt+1}): {type(e).__name__}: {e}")
            if "FLOOD" in err_str:
                wait_match = re.search(r"\d+", str(e))
                wait_time = int(wait_match.group()) + 10 if wait_match else 60
                await asyncio.sleep(wait_time)
                continue
            break

    # ===== 策略2: 下载重传 =====
    files_data = []
    file_names = []
    for msg in media_messages:
        data = await _download_media_bytes(client, msg)
        files_data.append(data)
        file_names.append(_get_file_name(msg))
        logging.info(f"[media_group] 下载 msg_id={getattr(msg, 'id', '?')}: {'OK ' + str(len(data)) + ' bytes' if data else 'FAILED'}")

    valid_indices = [i for i, d in enumerate(files_data) if d]
    if valid_indices:
        valid_data = [files_data[i] for i in valid_indices]
        try:
            result = await client.send_file(
                recipient, valid_data,
                caption=caption if caption else None,
                reply_to=reply_to,
                supports_streaming=True,
                force_document=False,
            )
            if result:
                logging.info(f"[media_group] 策略2成功")
                return result if isinstance(result, list) else [result]
        except Exception as e2:
            last_error = e2
            logging.warning(f"[media_group] 策略2失败: {type(e2).__name__}: {e2}")

    # ===== 策略3: 逐条 =====
    logging.warning("[media_group] 降级逐条发送")
    sent_list = []
    for i, msg in enumerate(media_messages):
        msg_caption = caption if i == 0 else ""
        msg_reply = reply_to if i == 0 else None
        sent = await _send_single_message(
            client, recipient, msg,
            caption=msg_caption,
            reply_to=msg_reply,
        )
        if sent:
            sent_list.append(sent)
        if i < len(media_messages) - 1:
            await asyncio.sleep(1)

    if sent_list:
        return sent_list

    logging.error(
        f"[media_group] 全部失败! types={media_types}, to={recipient}, "
        f"last_error={type(last_error).__name__}: {last_error}"
    )
    if caption and caption.strip():
        try:
            return await client.send_message(recipient, caption, reply_to=reply_to)
        except Exception:
            pass
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
    try:
        for item in os.listdir():
            if item.endswith(".session") or item.endswith(".session-journal"):
                try:
                    os.remove(item)
                except Exception as e:
                    logging.warning(f"无法删除 session 文件 {item}: {e}")
    except Exception as e:
        logging.warning(f"清理 session 文件失败: {e}")
