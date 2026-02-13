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
    return f"Running nb {__version__}\nPython {sys.version.replace(nl,'')}\nOS {os.name}\nPlatform {platform.system()} {platform.release()}\n{platform.architecture()} {platform.processor()}"


async def _download_and_send_media(client, recipient, message, caption=None, reply_to=None):
    """下载媒体后重新发送（最可靠的方式）"""
    try:
        # 下载媒体到内存
        media_bytes = await client.download_media(message, file=bytes)
        if not media_bytes:
            # 没有媒体，只发送文本
            if caption:
                return await client.send_message(recipient, caption, reply_to=reply_to)
            return None
        
        # 确定文件名和属性
        file_name = None
        if hasattr(message, 'file') and message.file:
            file_name = message.file.name
        
        # 发送下载的媒体
        return await client.send_file(
            recipient,
            media_bytes,
            caption=caption,
            reply_to=reply_to,
            file_name=file_name,
            supports_streaming=True,
            force_document=False
        )
    except Exception as e:
        logging.error(f"下载并发送媒体失败: {e}")
        # 降级：只发送文本
        if caption:
            try:
                return await client.send_message(recipient, caption, reply_to=reply_to)
            except:
                pass
        return None


async def _send_media_group(client, recipient, messages, caption=None, reply_to=None):
    """发送媒体组（下载后重新发送）"""
    try:
        files = []
        for msg in messages:
            if msg.media:
                try:
                    # 下载每个媒体
                    media_bytes = await client.download_media(msg, file=bytes)
                    if media_bytes:
                        files.append(media_bytes)
                except Exception as e:
                    logging.warning(f"下载媒体失败: {e}")
                    continue
        
        if not files:
            # 没有有效媒体，只发送文本
            if caption:
                return await client.send_message(recipient, caption, reply_to=reply_to)
            return None
        
        # 发送媒体组
        return await client.send_file(
            recipient,
            files,
            caption=caption,
            reply_to=reply_to,
            supports_streaming=True,
            force_document=False
        )
    except Exception as e:
        logging.error(f"发送媒体组失败: {e}")
        # 降级：只发送文本
        if caption:
            try:
                return await client.send_message(recipient, caption, reply_to=reply_to)
            except:
                pass
        return None


async def send_message(recipient, tm, grouped_messages=None, grouped_tms=None, comment_to_post=None):
    """发送消息（稳定版）"""
    client = tm.client
    effective_reply_to = comment_to_post if comment_to_post else tm.reply_to
    
    # ========== 转发模式 ==========
    if CONFIG.show_forwarded_from:
        if grouped_messages:
            try:
                return await client.forward_messages(recipient, grouped_messages)
            except Exception as e:
                logging.warning(f"转发失败，尝试下载重发: {e}")
                # 转发失败，尝试下载重发
        elif tm.message:
            try:
                return await client.forward_messages(recipient, tm.message)
            except Exception as e:
                logging.warning(f"转发失败，尝试下载重发: {e}")
    
    # ========== 媒体组 ==========
    if grouped_messages and grouped_tms:
        combined_caption = "\n\n".join([
            gtm.text.strip() for gtm in grouped_tms 
            if gtm.text and gtm.text.strip()
        ])
        
        for attempt in range(MAX_RETRIES):
            try:
                # 方法1：直接发送
                files = [msg for msg in grouped_messages if msg.media]
                if files:
                    result = await client.send_file(
                        recipient,
                        files,
                        caption=combined_caption or None,
                        reply_to=effective_reply_to,
                        supports_streaming=True,
                        force_document=False
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
                    continue
                elif "MEDIA" in err_str or "INVALID" in err_str or "FILE_REFERENCE" in err_str:
                    # 媒体无效，尝试下载重发
                    logging.warning(f"媒体无效，下载重发: {e}")
                    try:
                        return await _send_media_group(
                            client, recipient, grouped_messages,
                            caption=combined_caption,
                            reply_to=effective_reply_to
                        )
                    except Exception as e2:
                        logging.error(f"下载重发也失败: {e2}")
                else:
                    logging.error(f"媒体组发送失败 ({attempt+1}/{MAX_RETRIES}): {e}")
                
                await asyncio.sleep(5 * (attempt + 1))
        
        # 最后降级：只发送文本
        if combined_caption:
            try:
                return await client.send_message(recipient, combined_caption, reply_to=effective_reply_to)
            except:
                pass
        return None
    
    # ========== 单条消息 ==========
    text = tm.text or ""
    has_media = tm.message and tm.message.media
    
    # 如果有新处理过的文件
    if tm.new_file:
        try:
            return await client.send_file(
                recipient,
                tm.new_file,
                caption=text,
                reply_to=effective_reply_to,
                supports_streaming=True
            )
        except Exception as e:
            logging.error(f"发送处理过的文件失败: {e}")
            if text:
                try:
                    return await client.send_message(recipient, text, reply_to=effective_reply_to)
                except:
                    pass
            return None
    
    # 有媒体的消息
    if has_media:
        for attempt in range(MAX_RETRIES):
            try:
                # 方法1：直接发送
                return await client.send_message(
                    recipient,
                    text,
                    file=tm.message.media,
                    reply_to=effective_reply_to,
                    link_preview=False
                )
            except Exception as e:
                err_str = str(e).upper()
                if "FLOOD" in err_str:
                    wait_match = re.search(r"\d+", str(e))
                    wait_time = int(wait_match.group()) + 10 if wait_match else 60
                    logging.warning(f"FloodWait {wait_time}秒")
                    await asyncio.sleep(wait_time)
                    continue
                elif "MEDIA" in err_str or "INVALID" in err_str or "FILE_REFERENCE" in err_str:
                    # 方法2：下载后重新发送
                    logging.warning(f"媒体无效，下载重发: {e}")
                    try:
                        return await _download_and_send_media(
                            client, recipient, tm.message,
                            caption=text,
                            reply_to=effective_reply_to
                        )
                    except Exception as e2:
                        logging.error(f"下载重发失败: {e2}")
                else:
                    logging.error(f"发送失败 ({attempt+1}/{MAX_RETRIES}): {e}")
                
                await asyncio.sleep(5 * (attempt + 1))
        
        # 最后降级：只发送文本
        if text:
            try:
                return await client.send_message(recipient, text, reply_to=effective_reply_to)
            except:
                pass
        return None
    
    # 纯文本消息
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
