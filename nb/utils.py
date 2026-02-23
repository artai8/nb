# nb/utils.py —— 升级 Telethon 后的简化版本

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
from telethon.tl.functions.messages import SendMediaRequest, SendMultiMediaRequest

from nb import __version__
from nb.config import CONFIG
from nb.plugin_models import STYLE_CODES

if TYPE_CHECKING:
    from nb.plugins import NbMessage


MAX_RETRIES = 3


# =====================================================================
#  reply_to_msg_id 兼容辅助函数
# =====================================================================

def _get_reply_to_msg_id(message) -> Optional[int]:
    """兼容新旧版 Telethon 获取 reply_to_msg_id。"""
    if hasattr(message, 'reply_to_msg_id') and message.reply_to_msg_id is not None:
        return message.reply_to_msg_id
    if hasattr(message, 'reply_to') and message.reply_to is not None:
        if hasattr(message.reply_to, 'reply_to_msg_id'):
            return message.reply_to.reply_to_msg_id
    return None


# =====================================================================
#  Spoiler 检测与发送
# =====================================================================

def _has_spoiler(message: Message) -> bool:
    if not message or not message.media:
        return False
    return getattr(message.media, 'spoiler', False)


async def _send_single_with_spoiler(
    client: TelegramClient,
    recipient: EntityLike,
    message: Message,
    caption: Optional[str] = None,
    reply_to: Optional[int] = None,
) -> Message:
    media = message.media
    peer = await client.get_input_entity(recipient)

    if isinstance(media, MessageMediaPhoto) and media.photo:
        photo = media.photo
        input_media = InputMediaPhoto(
            id=InputPhoto(
                id=photo.id,
                access_hash=photo.access_hash,
                file_reference=photo.file_reference,
            ),
            spoiler=True,
        )
    elif isinstance(media, MessageMediaDocument) and media.document:
        doc = media.document
        input_media = InputMediaDocument(
            id=InputDocument(
                id=doc.id,
                access_hash=doc.access_hash,
                file_reference=doc.file_reference,
            ),
            spoiler=True,
        )
    else:
        raise ValueError(f"不支持的媒体类型: {type(media)}")

    result = await client(SendMediaRequest(
        peer=peer,
        media=input_media,
        message=caption or '',
        random_id=random.randrange(-2**63, 2**63),
        reply_to_msg_id=reply_to,
    ))

    if hasattr(result, 'updates'):
        for update in result.updates:
            if hasattr(update, 'message'):
                return update.message
    return result


async def _send_album_with_spoiler(
    client: TelegramClient,
    recipient: EntityLike,
    grouped_messages: List[Message],
    caption: Optional[str] = None,
    reply_to: Optional[int] = None,
) -> List[Message]:
    peer = await client.get_input_entity(recipient)
    multi_media = []

    for i, msg in enumerate(grouped_messages):
        media = msg.media
        is_spoiler = _has_spoiler(msg)
        msg_text = caption if (i == 0 and caption) else ""

        input_media = None

        if isinstance(media, MessageMediaPhoto) and media.photo:
            photo = media.photo
            input_media = InputMediaPhoto(
                id=InputPhoto(
                    id=photo.id,
                    access_hash=photo.access_hash,
                    file_reference=photo.file_reference,
                ),
                spoiler=is_spoiler,
            )
        elif isinstance(media, MessageMediaDocument) and media.document:
            doc = media.document
            input_media = InputMediaDocument(
                id=InputDocument(
                    id=doc.id,
                    access_hash=doc.access_hash,
                    file_reference=doc.file_reference,
                ),
                spoiler=is_spoiler,
            )

        if input_media is None:
            logging.warning(f"⚠️ 跳过无法识别的媒体类型: {type(media)}")
            continue

        single = InputSingleMedia(
            media=input_media,
            random_id=random.randrange(-2**63, 2**63),
            message=msg_text,
        )
        multi_media.append(single)

    if not multi_media:
        raise ValueError("没有有效的媒体可发送")

    kwargs = {
        'peer': peer,
        'multi_media': multi_media,
    }
    if reply_to is not None:
        kwargs['reply_to_msg_id'] = reply_to

    result = await client(SendMultiMediaRequest(**kwargs))

    sent_messages = []
    if hasattr(result, 'updates'):
        for update in result.updates:
            if hasattr(update, 'message'):
                sent_messages.append(update.message)

    logging.info(f"✅ 发送媒体组完成 ({len(multi_media)} 项)")
    return sent_messages if sent_messages else result


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
) -> Union[Message, List[Message], None]:
    """发送消息的统一入口。"""
    client: TelegramClient = tm.client

    # === 情况 1: 直接转发（保留 forwarded from） ===
    if CONFIG.show_forwarded_from and grouped_messages:
        attempt = 0
        delay = 5
        while attempt < MAX_RETRIES:
            try:
                result = await client.forward_messages(recipient, grouped_messages)
                logging.info(f"✅ 直接转发媒体组成功 (attempt {attempt+1})")
                return result
            except Exception as e:
                if "FLOOD_WAIT" in str(e).upper():
                    wait_match = re.search(r'\d+', str(e))
                    wait_sec = int(wait_match.group()) if wait_match else 30
                    logging.critical(f"⛔ FloodWait: 等待 {wait_sec} 秒")
                    await asyncio.sleep(wait_sec + 10)
                else:
                    logging.error(f"❌ 转发失败 (attempt {attempt+1}/{MAX_RETRIES}): {e}")
                attempt += 1
                delay = min(delay * 2, 300)
                await asyncio.sleep(delay)
        logging.error(f"❌ 直接转发最终失败，已重试 {MAX_RETRIES} 次")
        return None

    # === 情况 2: 媒体组复制发送 ===
    if grouped_messages and grouped_tms:
        combined_caption = "\n\n".join([
            gtm.text.strip() for gtm in grouped_tms
            if gtm.text and gtm.text.strip()
        ])

        any_spoiler = any(_has_spoiler(msg) for msg in grouped_messages)

        attempt = 0
        delay = 5
        while attempt < MAX_RETRIES:
            try:
                if any_spoiler:
                    logging.info("🔒 检测到 Spoiler，使用底层 API 发送")
                    result = await _send_album_with_spoiler(
                        client, recipient, grouped_messages,
                        caption=combined_caption or None,
                        reply_to=tm.reply_to,
                    )
                else:
                    files_to_send = [
                        msg for msg in grouped_messages
                        if msg.photo or msg.video or msg.gif or msg.document
                    ]
                    if not files_to_send:
                        return await client.send_message(
                            recipient,
                            combined_caption or "空相册",
                            reply_to=tm.reply_to,
                        )
                    result = await client.send_file(
                        recipient, files_to_send,
                        caption=combined_caption or None,
                        reply_to=tm.reply_to,
                        supports_streaming=True,
                        force_document=False,
                        allow_cache=False,
                        parse_mode="md",
                    )

                logging.info(
                    f"✅ 媒体组发送成功"
                    f"{'（含 spoiler）' if any_spoiler else ''}"
                    f" (attempt {attempt+1})"
                )
                return result

            except Exception as e:
                if "FLOOD_WAIT" in str(e).upper():
                    wait_match = re.search(r'\d+', str(e))
                    wait_sec = int(wait_match.group()) if wait_match else 30
                    logging.critical(f"⛔ FloodWait: 等待 {wait_sec} 秒")
                    await asyncio.sleep(wait_sec + 10)
                else:
                    logging.error(f"❌ 媒体组发送失败 (attempt {attempt+1}/{MAX_RETRIES}): {e}")
                attempt += 1
                delay = min(delay * 2, 300)
                await asyncio.sleep(delay)
        logging.error(f"❌ 媒体组发送最终失败，已重试 {MAX_RETRIES} 次")
        return None

    # === 情况 3: 单条消息 ===

    # 取出处理后的 reply_markup（可能为 None = 已移除）
    processed_markup = getattr(tm, 'reply_markup', None)

    # 3a: 插件生成了新文件
    if tm.new_file:
        try:
            return await client.send_file(
                recipient, tm.new_file,
                caption=tm.text,
                reply_to=tm.reply_to,
                supports_streaming=True,
                buttons=processed_markup,
            )
        except Exception as e:
            logging.warning(f"⚠️ 带按钮发送新文件失败，去除按钮重试: {e}")
            try:
                return await client.send_file(
                    recipient, tm.new_file,
                    caption=tm.text,
                    reply_to=tm.reply_to,
                    supports_streaming=True,
                )
            except Exception as e2:
                logging.error(f"❌ 新文件发送最终失败: {e2}")
                return None

    # 3b: 单条带 spoiler 的媒体
    if _has_spoiler(tm.message):
        logging.info("🔒 单条 Spoiler 消息，使用底层 API")
        try:
            result = await _send_single_with_spoiler(
                client, recipient, tm.message,
                caption=tm.text, reply_to=tm.reply_to,
            )
            logging.info("✅ 带 spoiler 单条消息发送成功")
            return result
        except Exception as e:
            logging.warning(f"⚠️ spoiler 发送失败，回退普通模式: {e}")

    # 3c: 普通消息 —— ★ 正确处理 reply_markup ★
    try:
        if processed_markup is not None:
            # 有处理后的按钮，用分离参数发送（避免原始 Message 对象携带旧 markup）
            try:
                return await client.send_message(
                    recipient,
                    tm.text,
                    file=tm.message.media if tm.message.media else None,
                    buttons=processed_markup,
                    reply_to=tm.reply_to,
                    link_preview=not bool(tm.message.media),
                )
            except Exception as e:
                logging.warning(f"⚠️ 带按钮发送失败，去除按钮重试: {e}")
                # 回退：不带按钮
                if tm.message.media:
                    return await client.send_message(
                        recipient,
                        tm.text,
                        file=tm.message.media,
                        reply_to=tm.reply_to,
                        link_preview=False,
                    )
                else:
                    return await client.send_message(
                        recipient,
                        tm.text,
                        reply_to=tm.reply_to,
                    )
        else:
            # markup 为 None —— 必须避免把原始 Message 的按钮带过去
            # 不能直接 send_message(recipient, tm.message)，因为 Message 对象内含原始 markup
            if tm.message.media:
                return await client.send_message(
                    recipient,
                    tm.text,
                    file=tm.message.media,
                    reply_to=tm.reply_to,
                    link_preview=False,
                )
            else:
                return await client.send_message(
                    recipient,
                    tm.text,
                    reply_to=tm.reply_to,
                )
    except Exception as e:
        logging.error(f"❌ 消息发送失败: {e}")
        return None


# =====================================================================
#  工具函数（不变）
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
    """Delete .session and .session-journal files."""
    for item in os.listdir():
        if item.endswith(".session") or item.endswith(".session-journal"):
            os.remove(item)
            logging.info(f"🧹 删除会话文件: {item}")
