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


async def _send_media_group_aligned(client, recipient, aligned_pairs, caption, reply_to):
    """发送媒体组 — 对齐版本
    
    aligned_pairs: [(message, tm), ...] 保持一一对应
    
    关键修复：
    1. 正确识别哪些 message 有媒体
    2. 确保 caption 附加到第一个有媒体的消息上
    3. 保持原始顺序或重新排序（caption 媒体必须在第一个）
    """
    if not aligned_pairs:
        return None

    # 分离媒体和纯文字，但保留对应关系
    media_pairs = [(m, tm) for m, tm in aligned_pairs if _msg_has_media(m)]
    text_pairs = [(m, tm) for m, tm in aligned_pairs if not _msg_has_media(m)]

    if not media_pairs:
        # 只有文本，直接发送 caption
        if caption and caption.strip():
            return await client.send_message(recipient, caption, reply_to=reply_to)
        return None

    # Telegram 限制：caption 只能放在第一个媒体
    # 找到应该带 caption 的媒体对
    caption_pair = None
    
    # 优先：tm 有 _caption_applied 且 text 非空
    for m, tm in media_pairs:
        if (getattr(tm, '_caption_applied', False) and tm.text):
            caption_pair = (m, tm)
            break
    
    # 其次：任意有 text 的媒体
    if not caption_pair:
        for m, tm in media_pairs:
            if tm.text:
                caption_pair = (m, tm)
                break
    
    # 最后：第一个媒体
    if not caption_pair:
        caption_pair = media_pairs[0]

    # 重新排序：caption 媒体放第一个
    ordered_pairs = [caption_pair]
    for pair in media_pairs:
        if pair is not caption_pair:
            ordered_pairs.append(pair)

    # 提取发送参数
    ordered_messages = [p[0] for p in ordered_pairs]
    ordered_tms = [p[1] for p in ordered_pairs]

    # Telegram caption 限制 1024 字符
    safe_caption = caption
    if safe_caption and len(safe_caption) > 1024:
        safe_caption = safe_caption[:1021] + "..."

    # 调用底层发送
    return await _send_media_group_impl(
        client, recipient, ordered_messages, ordered_tms,
        caption=safe_caption, reply_to=reply_to
    )


async def _send_media_group_impl(client, recipient, media_messages, tms, caption, reply_to):
    """底层媒体组实现 — 使用 message.media 或下载重传"""
    
    # 策略1: 直接用 message.media 列表发送（保持相册效果）
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
            return result if isinstance(result, list) else [result]
    except Exception as e:
        err_str = str(e).upper()
        if "FLOOD" in err_str:
            wait_match = re.search(r"\d+", str(e))
            wait_time = int(wait_match.group()) + 10 if wait_match else 60
            logging.warning(f"FloodWait {wait_time}s (媒体组策略1)")
            await asyncio.sleep(wait_time)
            # 重试策略1
            try:
                result = await client.send_file(
                    recipient, media_list,
                    caption=caption if caption else None,
                    reply_to=reply_to,
                    supports_streaming=True,
                    force_document=False,
                )
                if result:
                    return result if isinstance(result, list) else [result]
            except Exception as e2:
                logging.warning(f"媒体组策略1重试失败: {e2}")
        else:
            logging.warning(f"媒体组策略1失败: {e}")

    # 策略2: 下载为 bytes 后重新上传
    files_data = []
    file_names = []
    for msg, tm in zip(media_messages, tms):
        data = await _download_media_bytes(client, msg)
        files_data.append(data)
        file_names.append(_get_file_name(msg))

    valid_files = [(d, n) for d, n in zip(files_data, file_names) if d]
    if valid_files:
        try:
            result = await client.send_file(
                recipient, [d for d, n in valid_files],
                caption=caption if caption else None,
                reply_to=reply_to,
                supports_streaming=True,
                force_document=False,
            )
            if result:
                return result if isinstance(result, list) else [result]
        except Exception as e2:
            logging.warning(f"媒体组策略2失败: {e2}")

    # 策略3: 逐条发送（保底）
    logging.warning("媒体组降级为逐条发送")
    sent_list = []
    for i, (msg, tm) in enumerate(zip(media_messages, tms)):
        msg_caption = caption if i == 0 else ""
        msg_reply = reply_to if i == 0 else None
        
        sent = await _send_single_message(
            client, recipient, msg,
            caption=msg_caption,
            reply_to=msg_reply,
        )
        if sent:
            sent_list.append(sent)
        
        # 逐条发送之间加延迟避免 flood
        if i < len(media_messages) - 1:
            await asyncio.sleep(1)

    return sent_list if sent_list else None


async def send_message(recipient, tm, grouped_messages=None, grouped_tms=None, comment_to_post=None):
    """发送消息主函数 — 完全修复版
    
    关键修复：
    1. 正确处理 _caption_applied 标记
    2. 确保 caption 和 media 正确对应
    3. 混合媒体组中，找到真正有 caption 的媒体
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
        # 关键修复：对齐检查
        if len(grouped_messages) != len(grouped_tms):
            logging.warning(f"消息数量不匹配: messages={len(grouped_messages)}, tms={len(grouped_tms)}")
            # 尝试用 tm.message 重建对应关系
            grouped_messages = [tm.message for tm in grouped_tms]

        # 检查 caption 插件是否已经处理过
        caption_already_applied = any(
            getattr(gtm, '_caption_applied', False) for gtm in grouped_tms
        )

        if caption_already_applied:
            # 关键修复：找到设置了 caption 的媒体消息
            # 优先找：有媒体 + text 非空 + _caption_applied
            caption_tm = None
            for gtm in grouped_tms:
                if (gtm.file_type != "nofile" and 
                    gtm.text and 
                    getattr(gtm, '_caption_applied', False)):
                    caption_tm = gtm
                    break
            
            # 如果没找到，找任意有 text 且 _caption_applied 的
            if not caption_tm:
                for gtm in grouped_tms:
                    if gtm.text and getattr(gtm, '_caption_applied', False):
                        caption_tm = gtm
                        break
            
            # 最后兜底：第一个有媒体或有 text 的
            if not caption_tm:
                for gtm in grouped_tms:
                    if gtm.file_type != "nofile" or gtm.text:
                        caption_tm = gtm
                        break
            
            combined_caption = caption_tm.text if caption_tm else None
            
            # 调试日志
            logging.debug(f"Caption applied: target={caption_tm.file_type if caption_tm else 'None'}, "
                         f"text_len={len(combined_caption) if combined_caption else 0}")
        else:
            # 未经 caption 插件处理，正常收集所有文本
            all_texts = []
            for gtm in grouped_tms:
                content = gtm.text or gtm.raw_text or ""
                if content.strip():
                    all_texts.append(content.strip())
            combined_caption = "\n\n".join(all_texts) if all_texts else None

        # 关键修复：构建对齐的 (message, tm) 对，用于发送
        aligned_pairs = list(zip(grouped_messages, grouped_tms))
        
        return await _send_media_group_aligned(
            client, recipient, aligned_pairs,
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
