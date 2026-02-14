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
#  判断是否需要 copy
# =====================================================================

def _plugins_modified(tm: "NbMessage") -> bool:
    if tm.new_file:
        return True
    original_text = tm.message.text or ""
    current_text = tm.text or ""
    if original_text != current_text:
        return True
    msg_client = getattr(tm.message, '_client', None) or getattr(tm.message, 'client', None)
    if msg_client is not None and tm.client is not msg_client:
        return True
    return False


def _get_download_client(tm: "NbMessage") -> TelegramClient:
    msg_client = getattr(tm.message, '_client', None) or getattr(tm.message, 'client', None)
    if msg_client is not None:
        return msg_client
    return tm.client


# =====================================================================
#  刷新消息（获取新的 file_reference）
# =====================================================================

async def _refresh_message(
    client: TelegramClient,
    message: Message,
) -> Message:
    """从源频道重新获取消息，刷新 file_reference。
    如果刷新失败则返回原始消息。
    """
    try:
        refreshed = await client.get_messages(message.chat_id, ids=message.id)
        if refreshed:
            logging.debug(f"🔄 消息 {message.id} file_reference 已刷新")
            return refreshed
    except Exception as e:
        logging.warning(f"⚠️ 刷新消息 {message.id} 失败: {e}")
    return message


async def _refresh_messages(
    client: TelegramClient,
    messages: List[Message],
) -> List[Message]:
    """批量刷新消息列表。"""
    if not messages:
        return messages
    chat_id = messages[0].chat_id
    msg_ids = [m.id for m in messages]
    try:
        refreshed = await client.get_messages(chat_id, ids=msg_ids)
        if refreshed:
            # get_messages 返回的顺序和 ids 一致
            result = []
            for i, r in enumerate(refreshed if isinstance(refreshed, list) else [refreshed]):
                if r:
                    result.append(r)
                else:
                    result.append(messages[i])
            logging.debug(f"🔄 批量刷新 {len(result)} 条消息成功")
            return result
    except Exception as e:
        logging.warning(f"⚠️ 批量刷新失败: {e}")
    return messages


# =====================================================================
#  forward 原样转发（带 "Forwarded from"）
# =====================================================================

async def _forward_single(
    client: TelegramClient,
    recipient: EntityLike,
    message: Message,
) -> Optional[Message]:
    for attempt in range(MAX_RETRIES):
        try:
            result = await client.forward_messages(
                recipient, message.id, from_peer=message.chat_id,
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
    logging.error("❌ forward 媒体组最终失败")
    return None


# =====================================================================
#  copy 方式发送（不带 "Forwarded from"）
#  核心方法：先刷新消息拿到新 file_reference，再用 send_message(file=media)
# =====================================================================

async def _copy_single(
    send_client: TelegramClient,
    download_client: TelegramClient,
    recipient: EntityLike,
    tm: "NbMessage",
    reply_to: Optional[int] = None,
) -> Optional[Message]:
    """复制发送单条消息，不带来源标记。"""
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

    # ★ 有媒体 → 刷新消息拿新 file_reference，再用 send_message(file=media)
    refreshed = await _refresh_message(download_client, tm.message)

    for attempt in range(MAX_RETRIES):
        try:
            if processed_markup is not None:
                try:
                    result = await send_client.send_message(
                        recipient, tm.text,
                        file=refreshed.media,
                        buttons=processed_markup,
                        reply_to=reply_to,
                        link_preview=False,
                    )
                    logging.info(f"✅ copy 成功(带按钮) msg={tm.message.id} (attempt {attempt+1})")
                    return result
                except Exception as e_btn:
                    logging.warning(f"⚠️ 带按钮发送失败: {e_btn}")

            result = await send_client.send_message(
                recipient, tm.text,
                file=refreshed.media,
                reply_to=reply_to,
                link_preview=False,
            )
            logging.info(f"✅ copy 成功 msg={tm.message.id} (attempt {attempt+1})")
            return result

        except Exception as e:
            if _is_flood_wait(e):
                await _handle_flood_wait(e)
            else:
                logging.warning(f"⚠️ copy 失败 (attempt {attempt+1}/{MAX_RETRIES}): {e}")
                # 如果还是 file_reference 错误，再刷新一次
                if attempt < MAX_RETRIES - 1:
                    refreshed = await _refresh_message(download_client, tm.message)
                await asyncio.sleep(RETRY_BASE_DELAY * (attempt + 1))

    # 全部失败 → 降级 forward
    logging.warning("⚠️ copy 全部失败，降级为 forward（会带来源标记）")
    return await _forward_single(send_client, recipient, tm.message)


async def _copy_album(
    send_client: TelegramClient,
    download_client: TelegramClient,
    recipient: EntityLike,
    messages: List[Message],
    tms: Optional[List["NbMessage"]] = None,
    reply_to: Optional[int] = None,
) -> Optional[List[Message]]:
    """复制发送媒体组，不带来源标记。"""
    if tms:
        combined_caption = "\n\n".join([
            gtm.text.strip() for gtm in tms
            if gtm.text and gtm.text.strip()
        ])
    else:
        combined_caption = "\n\n".join([
            (m.text or "").strip() for m in messages
            if (m.text or "").strip()
        ])

    # ★ 刷新所有消息拿新 file_reference
    refreshed_msgs = await _refresh_messages(download_client, messages)

    files_to_send = [
        msg for msg in refreshed_msgs
        if msg.media and (msg.photo or msg.video or msg.gif or msg.document)
    ]

    if not files_to_send:
        # 没有可发送的媒体，发纯文本
        try:
            return await send_client.send_message(
                recipient, combined_caption or "空相册", reply_to=reply_to,
            )
        except Exception as e:
            logging.error(f"❌ 纯文本发送失败: {e}")
            return None

    for attempt in range(MAX_RETRIES):
        try:
            result = await send_client.send_file(
                recipient, files_to_send,
                caption=combined_caption or None,
                reply_to=reply_to,
                supports_streaming=True,
                force_document=False,
                allow_cache=False,
                parse_mode="md",
            )
            if not isinstance(result, list):
                result = [result]
            logging.info(f"✅ copy 媒体组成功 ({len(files_to_send)} 项, attempt {attempt+1})")
            return result

        except Exception as e:
            if _is_flood_wait(e):
                await _handle_flood_wait(e)
            else:
                logging.warning(f"⚠️ copy 媒体组失败 (attempt {attempt+1}/{MAX_RETRIES}): {e}")
                # 再刷新一次
                if attempt < MAX_RETRIES - 1:
                    refreshed_msgs = await _refresh_messages(download_client, messages)
                    files_to_send = [
                        msg for msg in refreshed_msgs
                        if msg.media and (msg.photo or msg.video or msg.gif or msg.document)
                    ]
                await asyncio.sleep(RETRY_BASE_DELAY * (attempt + 1))

    # 降级 forward
    logging.warning("⚠️ copy 媒体组全部失败，降级为 forward（会带来源标记）")
    return await _forward_album(send_client, recipient, messages)


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

    策略:
      - show_forwarded_from=True 且无插件修改 且非评论区 → forward（保留来源）
      - 其他情况 → copy（刷新 file_reference + send_message/send_file，无来源）
    """
    send_client: TelegramClient = tm.client
    download_client: TelegramClient = _get_download_client(tm)
    effective_reply_to = comment_to_post if comment_to_post else tm.reply_to

    # 评论区必须 copy（forward 不支持 reply_to 到评论帖子）
    force_copy = comment_to_post is not None
    need_copy = force_copy or _plugins_modified(tm) or (not CONFIG.show_forwarded_from)

    # === 媒体组 ===
    if grouped_messages:
        group_need_copy = force_copy or (not CONFIG.show_forwarded_from)
        if not group_need_copy and grouped_tms:
            for gtm in grouped_tms:
                if _plugins_modified(gtm):
                    group_need_copy = True
                    break

        if group_need_copy:
            logging.info("📦 媒体组 → copy")
            return await _copy_album(
                send_client, download_client,
                recipient, grouped_messages, grouped_tms,
                reply_to=effective_reply_to,
            )
        else:
            logging.info("📦 媒体组 → forward")
            return await _forward_album(send_client, recipient, grouped_messages)

    # === 单条消息 ===
    if need_copy:
        logging.info(f"📝 msg={tm.message.id} → copy")
        return await _copy_single(
            send_client, download_client,
            recipient, tm, reply_to=effective_reply_to,
        )
    else:
        logging.info(f"📨 msg={tm.message.id} → forward")
        return await _forward_single(send_client, recipient, tm.message)


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
