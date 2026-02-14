# nb/utils.py

import logging
import asyncio
import re
import os
import sys
import platform
import random
from datetime import datetime
from typing import TYPE_CHECKING, List, Optional, Union

from telethon.client import TelegramClient
from telethon.hints import EntityLike
from telethon.tl.custom.message import Message
from telethon.tl.types import (
    InputMediaPhoto,
    InputMediaDocument,
    InputPhoto,
    InputDocument,
    InputSingleMedia,
    MessageMediaPhoto,
    MessageMediaDocument,
)
from telethon.tl.functions.messages import SendMediaRequest, SendMultiMediaRequest

from nb import __version__
from nb.config import CONFIG
from nb.plugin_models import STYLE_CODES

if TYPE_CHECKING:
    from nb.plugins import NbMessage

MAX_RETRIES = 3

# =====================================================================
#  关键修复：添加被引用的工具函数
# =====================================================================

def _extract_msg_id(fwded) -> Optional[int]:
    """安全提取消息 ID，兼容 Message、int、list 等类型。"""
    if fwded is None:
        return None
    if isinstance(fwded, int):
        return fwded
    if isinstance(fwded, list):
        if fwded and hasattr(fwded[0], 'id'):
            return fwded[0].id
        return None
    if hasattr(fwded, 'id'):
        return fwded.id
    return None

def _get_reply_to_msg_id(message) -> Optional[int]:
    """兼容新旧版 Telethon 获取 reply_to_msg_id。"""
    if hasattr(message, 'reply_to_msg_id') and message.reply_to_msg_id is not None:
        return message.reply_to_msg_id
    if hasattr(message, 'reply_to') and message.reply_to is not None:
        if hasattr(message.reply_to, 'reply_to_msg_id'):
            return message.reply_to.reply_to_msg_id
    return None

def _has_spoiler(message: Message) -> bool:
    if not message or not message.media:
        return False
    return getattr(message.media, 'spoiler', False)

# =====================================================================
#  发送逻辑
# =====================================================================

async def send_message(
    recipient: EntityLike,
    tm: "NbMessage",
    grouped_messages: Optional[List[Message]] = None,
    grouped_tms: Optional[List["NbMessage"]] = None,
) -> Union[Message, List[Message], None]:
    """发送消息的统一入口。"""
    client: TelegramClient = tm.client

    # 1. 直接转发模式
    if CONFIG.show_forwarded_from and grouped_messages:
        try:
            return await client.forward_messages(recipient, grouped_messages)
        except Exception as e:
            logging.error(f"❌ 转发失败: {e}")
            return None

    # 2. 媒体组复制模式
    if grouped_messages and grouped_tms:
        combined_caption = "\n\n".join([gtm.text.strip() for gtm in grouped_tms if gtm.text and gtm.text.strip()])
        try:
            # 简化发送，由 Telethon 自动处理相册
            return await client.send_file(
                recipient, 
                [m for m in grouped_messages if m.media], 
                caption=combined_caption or None, 
                reply_to=tm.reply_to, 
                supports_streaming=True
            )
        except Exception as e:
            logging.error(f"❌ 媒体组发送失败: {e}")
            return None

    # 3. 单条消息模式
    processed_markup = getattr(tm, 'reply_markup', None)
    try:
        if tm.new_file:
            return await client.send_file(recipient, tm.new_file, caption=tm.text, reply_to=tm.reply_to, buttons=processed_markup)
        
        return await client.send_message(
            recipient, 
            tm.text, 
            file=tm.message.media if tm.message.media else None, 
            buttons=processed_markup, 
            reply_to=tm.reply_to, 
            link_preview=not bool(tm.message.media)
        )
    except Exception as e:
        logging.error(f"❌ 消息发送失败: {e}")
        return None

# =====================================================================
#  基础工具函数
# =====================================================================

def platform_info():
    return f"nb {__version__}\nPython {sys.version}\nPlatform {platform.system()}"

def cleanup(*files):
    for f in files:
        try: os.remove(f)
        except: pass

def stamp(file, user):
    now = str(datetime.now()).replace(":", "-")
    outf = f"{user}_{now}_{file}"
    try:
        os.rename(file, outf)
        return outf
    except: return file

def safe_name(string):
    return re.sub(r"[-!@#$%^&*()\s]", "_", string)

def match(pattern, string, regex):
    if regex: return bool(re.findall(pattern, string))
    return pattern in string

def replace(pattern, new, string, regex):
    if regex: return re.sub(pattern, new, string)
    return string.replace(pattern, new)

def clean_session_files():
    for item in os.listdir():
        if item.endswith(".session") or item.endswith(".session-journal"):
            try: os.remove(item)
            except: pass
