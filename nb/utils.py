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
    DocumentAttributeVideo,
    DocumentAttributeAnimated,
    DocumentAttributeAudio,
    DocumentAttributeSticker,
    InputMediaPhoto,
    InputMediaDocument,
    InputPhoto,
    InputDocument,
    InputSingleMedia,
    MessageMediaPhoto,
    MessageMediaDocument,
)
from telethon.tl.functions.messages import (
    SendMediaRequest,
    SendMultiMediaRequest,
    GetDiscussionMessageRequest,
)

from nb import __version__
from nb.config import CONFIG
from nb.plugin_models import STYLE_CODES

if TYPE_CHECKING:
    from nb.plugins import NbMessage


MAX_RETRIES = 5
RETRY_BASE_DELAY = 5


# =====================================================================
#  reply_to 兼容辅助
# =====================================================================

def _get_reply_to_msg_id(message) -> Optional[int]:
    if hasattr(message, 'reply_to_msg_id') and message.reply_to_msg_id is not None:
        return message.reply_to_msg_id
    if hasattr(message, 'reply_to') and message.reply_to is not None:
        if hasattr(message.reply_to, 'reply_to_msg_id'):
            return message.reply_to.reply_to_msg_id
    return None


def _get_reply_to_top_id(message) -> Optional[int]:
    reply_to = getattr(message, 'reply_to', None)
    if reply_to is None:
        return None
    return getattr(reply_to, 'reply_to_top_id', None)


async def get_discussion_message(
    client: TelegramClient,
    channel_id: Union[int, str],
    msg_id: int,
) -> Optional[Message]:
    try:
        result = await client(GetDiscussionMessageRequest(
            peer=channel_id, msg_id=msg_id,
        ))
        if result and result.messages:
            return result.messages[0]
    except Exception as e:
        logging.warning(f"⚠️ 获取讨论消息失败 (channel={channel_id}, msg={msg_id}): {e}")
    return None


async def get_discussion_group_id(
    client: TelegramClient,
    channel_id: Union[int, str],
) -> Optional[int]:
    try:
        full = await client.get_entity(channel_id)
        if hasattr(full, 'linked_chat_id') and full.linked_chat_id:
            return full.linked_chat_id
        from telethon.tl.functions.channels import GetFullChannelRequest
        full_channel = await client(GetFullChannelRequest(channel_id))
        if hasattr(full_channel.full_chat, 'linked_chat_id'):
            return full_channel.full_chat.linked_chat_id
    except Exception as e:
        logging.warning(f"⚠️ 获取讨论组失败 (channel={channel_id}): {e}")
    return None


# =====================================================================
#  FloodWait
# =====================================================================

def _is_flood_wait(e: Exception) -> bool:
    return "FLOOD_WAIT" in str(e).upper() or "flood" in str(e).lower()


async def _handle_flood_wait(e: Exception) -> int:
    wait_match = re.search(r'(\d+)', str(e))
    wait_sec = int(wait_match.group()) if wait_match else 30
    logging.critical(f"⛔ FloodWait: 等待 {wait_sec + 10} 秒")
    await asyncio.sleep(wait_sec + 10)
    return wait_sec


# =====================================================================
#  Spoiler
# =====================================================================

def _has_spoiler(message: Message) -> bool:
    if not message or not message.media:
        return False
    return getattr(message.media, 'spoiler', False)


# =====================================================================
#  判断插件是否修改了消息
# =====================================================================

def _plugins_modified(tm: "NbMessage") -> bool:
    """判断插件是否修改了消息内容。
    如果没有修改，可以直接 forward，最安全最可靠。
    """
    if tm.new_file:
        return True

    original_text = tm.message.text or ""
    current_text = tm.text or ""
    if original_text != current_text:
        return True

    original_markup = tm.message.reply_markup
    current_markup = getattr(tm, 'reply_markup', None)
    # 按钮被移除了
    if original_markup is not None and current_markup is None:
        return True

    # sender 插件替换了 client
    msg_client = getattr(tm.message, '_client', None) or getattr(tm.message, 'client', None)
    if msg_client is not None and tm.client is not msg_client:
        return True

    return False


# =====================================================================
#  forward 原样转发（最可靠）
# =====================================================================

async def _forward_single(
    client: TelegramClient,
    recipient: EntityLike,
    message: Message,
) -> Optional[Message]:
    """用 forward_messages 原样转发单条消息。"""
    for attempt in range(MAX_RETRIES):
        try:
            result = await client.forward_messages(
                recipient,
                message.id,
                from_peer=message.chat_id,
            )
            if isinstance(result, list):
                result = result[0] if result else None
            logging.info(f"✅ forward 成功 msg={message.id} (attempt {attempt+1})")
            return result
        except Exception as e:
            if _is_flood_wait(e):
                await _handle_flood_wait(e)
            else:
                logging.warning(f"⚠️ forward 失败 (attempt {attempt+1}/{MAX_RETRIES}): {e}")
                await asyncio.sleep(RETRY_BASE_DELAY * (attempt + 1))
    logging.error(f"❌ forward 最终失败 msg={message.id}")
    return None


async def _forward_album(
    client: TelegramClient,
    recipient: EntityLike,
    messages: List[Message],
) -> Optional[List[Message]]:
    """用 forward_messages 原样转发媒体组。"""
    msg_ids = [m.id for m in messages]
    from_peer = messages[0].chat_id
    for attempt in range(MAX_RETRIES):
        try:
            result = await client.forward_messages(
                recipient, msg_ids, from_peer=from_peer,
            )
            if not isinstance(result, list):
                result = [result]
            logging.info(f"✅ forward 媒体组成功 ({len(msg_ids)} 条, attempt {attempt+1})")
            return result
        except Exception as e:
            if _is_flood_wait(e):
                await _handle_flood_wait(e)
            else:
                logging.warning(f"⚠️ forward 媒体组失败 (attempt {attempt+1}/{MAX_RETRIES}): {e}")
                await asyncio.sleep(RETRY_BASE_DELAY * (attempt + 1))
    logging.error(f"❌ forward 媒体组最终失败")
    return None


# =====================================================================
#  copy 方式发送（下载+上传，用于插件修改了内容时）
# =====================================================================

def _get_download_client(tm: "NbMessage") -> TelegramClient:
    msg_client = getattr(tm.message, '_client', None) or getattr(tm.message, 'client', None)
    if msg_client is not None:
        return msg_client
    return tm.client


async def _download_media_robust(
    download_client: TelegramClient,
    message: Message,
) -> Optional[bytes]:
    """多种方式尝试下载媒体。"""
    chat_id = message.chat_id
    msg_id = message.id

    # 方法1: 刷新消息 + download_media(bytes)
    try:
        refreshed = await download_client.get_messages(chat_id, ids=msg_id)
        if refreshed and refreshed.media:
            data = await refreshed.download_media(file=bytes)
            if data:
                logging.info(f"✅ 下载成功(刷新+bytes) msg={msg_id} ({len(data)} bytes)")
                return data
    except Exception as e:
        logging.debug(f"方法1失败: {e}")

    # 方法2: 刷新消息 + download_media(临时文件)
    try:
        refreshed = await download_client.get_messages(chat_id, ids=msg_id)
        if refreshed and refreshed.media:
            temp_path = await refreshed.download_media(file="")
            if temp_path and os.path.exists(temp_path):
                with open(temp_path, "rb") as f:
                    data = f.read()
                os.remove(temp_path)
                if data:
                    logging.info(f"✅ 下载成功(刷新+file) msg={msg_id} ({len(data)} bytes)")
                    return data
    except Exception as e:
        logging.debug(f"方法2失败: {e}")

    # 方法3: client.download_media 显式调用
    try:
        refreshed = await download_client.get_messages(chat_id, ids=msg_id)
        if refreshed:
            data = await download_client.download_media(refreshed, file=bytes)
            if data:
                logging.info(f"✅ 下载成功(client.download) msg={msg_id} ({len(data)} bytes)")
                return data
    except Exception as e:
        logging.debug(f"方法3失败: {e}")

    # 方法4: 原始消息对象
    try:
        data = await message.download_media(file=bytes)
        if data:
            logging.info(f"✅ 下载成功(原始) msg={msg_id} ({len(data)} bytes)")
            return data
    except Exception as e:
        logging.debug(f"方法4失败: {e}")

    try:
        temp_path = await message.download_media(file="")
        if temp_path and os.path.exists(temp_path):
            with open(temp_path, "rb") as f:
                data = f.read()
            os.remove(temp_path)
            if data:
                logging.info(f"✅ 下载成功(原始+file) msg={msg_id} ({len(data)} bytes)")
                return data
    except Exception as e:
        logging.debug(f"方法5失败: {e}")

    logging.error(f"❌ 所有下载方式均失败 msg={msg_id}")
    return None


async def _copy_single(
    send_client: TelegramClient,
    download_client: TelegramClient,
    recipient: EntityLike,
    tm: "NbMessage",
    reply_to: Optional[int] = None,
) -> Optional[Message]:
    """复制发送单条消息（下载媒体+上传）。失败则降级为 forward。"""
    processed_markup = getattr(tm, 'reply_markup', None)

    # 插件生成了新文件
    if tm.new_file:
        try:
            return await send_client.send_file(
                recipient, tm.new_file,
                caption=tm.text, reply_to=reply_to,
                supports_streaming=True, buttons=processed_markup,
            )
        except Exception:
            try:
                return await send_client.send_file(
                    recipient, tm.new_file,
                    caption=tm.text, reply_to=reply_to,
                    supports_streaming=True,
                )
            except Exception as e2:
                logging.error(f"❌ 新文件发送失败: {e2}")
                return None

    # 无媒体 → 纯文本
    if not tm.message.media:
        try:
            return await send_client.send_message(
                recipient, tm.text, reply_to=reply_to,
            )
        except Exception as e:
            logging.error(f"❌ 纯文本发送失败: {e}")
            return None

    # 有媒体 → 下载后上传
    file_bytes = await _download_media_robust(download_client, tm.message)
    if file_bytes:
        for attempt in range(MAX_RETRIES):
            try:
                result = await send_client.send_file(
                    recipient, file_bytes,
                    caption=tm.text, reply_to=reply_to,
                    supports_streaming=True, buttons=processed_markup,
                )
                logging.info(f"✅ copy 单条成功 (attempt {attempt+1})")
                return result
            except Exception as e:
                if _is_flood_wait(e):
                    await _handle_flood_wait(e)
                else:
                    logging.warning(f"⚠️ copy 失败 (attempt {attempt+1}): {e}")
                    if processed_markup is not None:
                        try:
                            return await send_client.send_file(
                                recipient, file_bytes,
                                caption=tm.text, reply_to=reply_to,
                                supports_streaming=True,
                            )
                        except Exception:
                            pass
                    await asyncio.sleep(RETRY_BASE_DELAY * (attempt + 1))

    # 全部失败 → 降级 forward
    logging.warning("⚠️ copy 失败，降级为 forward")
    return await _forward_single(send_client, recipient, tm.message)


async def _copy_album(
    send_client: TelegramClient,
    download_client: TelegramClient,
    recipient: EntityLike,
    grouped_messages: List[Message],
    grouped_tms: List["NbMessage"],
    reply_to: Optional[int] = None,
) -> Optional[List[Message]]:
    """复制发送媒体组。失败则降级为 forward。"""
    combined_caption = "\n\n".join([
        gtm.text.strip() for gtm in grouped_tms
        if gtm.text and gtm.text.strip()
    ])

    downloaded = []
    for msg in grouped_messages:
        if msg.media and (msg.photo or msg.video or msg.gif or msg.document):
            data = await _download_media_robust(download_client, msg)
            if data:
                downloaded.append(data)

    if downloaded:
        for attempt in range(MAX_RETRIES):
            try:
                result = await send_client.send_file(
                    recipient, downloaded,
                    caption=combined_caption or None,
                    reply_to=reply_to,
                    supports_streaming=True,
                    force_document=False, allow_cache=False,
                )
                if not isinstance(result, list):
                    result = [result]
                logging.info(f"✅ copy 媒体组成功 ({len(downloaded)} 项, attempt {attempt+1})")
                return result
            except Exception as e:
                if _is_flood_wait(e):
                    await _handle_flood_wait(e)
                else:
                    logging.warning(f"⚠️ copy 媒体组失败 (attempt {attempt+1}): {e}")
                    await asyncio.sleep(RETRY_BASE_DELAY * (attempt + 1))

    logging.warning("⚠️ copy 媒体组失败，降级为 forward")
    return await _forward_album(send_client, recipient, grouped_messages)


# =====================================================================
#  主发送函数
# =====================================================================

def platform_info():
    nl = "\n"
    return f"""Running nb {__version__}\
    \nPython {sys.version.replace(nl,"")}\
    \nOS {os.name}\
    \nPlatform {platform.system()} {platform.release()}\
    \n{platform.architecture()} {platform.processor()}"""


async def send_message(
    recipient: EntityLike,
    tm: "NbMessage",
    grouped_messages: Optional[List[Message]] = None,
    grouped_tms: Optional[List["NbMessage"]] = None,
    comment_to_post: Optional[int] = None,
) -> Union[Message, List[Message], None]:
    """发送消息的统一入口。

    核心策略:
      - 插件没有修改 → forward_messages（最可靠，不受 file_reference 影响）
      - 插件修改了    → copy（下载+上传），失败则降级 forward
    """
    send_client: TelegramClient = tm.client
    download_client: TelegramClient = _get_download_client(tm)
    effective_reply_to = comment_to_post if comment_to_post else tm.reply_to
    modified = _plugins_modified(tm)

    # === 媒体组 ===
    if grouped_messages:
        group_modified = False
        if grouped_tms:
            for gtm in grouped_tms:
                if _plugins_modified(gtm):
                    group_modified = True
                    break

        if not group_modified:
            logging.info("📦 媒体组未修改 → forward")
            return await _forward_album(send_client, recipient, grouped_messages)
        else:
            logging.info("📦 媒体组已修改 → copy")
            return await _copy_album(
                send_client, download_client,
                recipient, grouped_messages, grouped_tms,
                reply_to=effective_reply_to,
            )

    # === 单条消息 ===
    if not modified:
        logging.info(f"📨 msg={tm.message.id} 未修改 → forward")
        return await _forward_single(send_client, recipient, tm.message)
    else:
        logging.info(f"📝 msg={tm.message.id} 已修改 → copy")
        return await _copy_single(
            send_client, download_client,
            recipient, tm, reply_to=effective_reply_to,
        )


# =====================================================================
#  工具函数
# =====================================================================

def cleanup(*files: str) -> None:
    for file in files:
        try:
            os.remove(file)
        except FileNotFoundError:
            logging.info(f"File {file} does not exist.")


def stamp(file: str, user: str) -> str:
    now = str(datetime.now())
    outf = safe_name(f"{user} {now} {file}")
    try:
        os.rename(file, outf)
        return outf
    except Exception as err:
        logging.warning(f"重命名失败 {file} → {outf}: {err}")
        return file


def safe_name(string: str) -> str:
    return re.sub(pattern=r"[-!@#$%^&*()\s]", repl="_", string=string)


def match(pattern: str, string: str, regex: bool) -> bool:
    if regex:
        return bool(re.findall(pattern, string))
    return pattern in string


def replace(pattern: str, new: str, string: str, regex: bool) -> str:
    def fmt_repl(matched):
        style = new
        code = STYLE_CODES.get(style)
        return f"{code}{matched.group(0)}{code}" if code else new

    if regex:
        if new in STYLE_CODES:
            compiled_pattern = re.compile(pattern)
            return compiled_pattern.sub(repl=fmt_repl, string=string)
        return re.sub(pattern, new, string)
    else:
        if new in STYLE_CODES:
            code = STYLE_CODES[new]
            return string.replace(pattern, f"{code}{pattern}{code}")
        return string.replace(pattern, new)


def clean_session_files():
    for item in os.listdir():
        if item.endswith(".session") or item.endswith(".session-journal"):
            os.remove(item)
            logging.info(f"🧹 删除会话文件: {item}")
