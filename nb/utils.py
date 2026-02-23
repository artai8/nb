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
    ReplyInlineMarkup,
    KeyboardButtonUrl,
    KeyboardButtonCallback,
)
from telethon.tl.functions.messages import (
    SendMediaRequest,
    SendMultiMediaRequest,
    GetDiscussionMessageRequest,
)
from telethon.tl.functions.channels import GetFullChannelRequest

from nb import __version__
from nb.config import CONFIG
from nb.plugin_models import STYLE_CODES

if TYPE_CHECKING:
    from nb.plugins import NbMessage


MAX_RETRIES = 3


# =====================================================================
#  reply_to 兼容辅助函数
# =====================================================================

def _build_reply_to(msg_id: Optional[int]):
    """
    构造 reply_to 参数，兼容新旧版 Telethon。
    新版需要 InputReplyToMessage 对象，旧版直接用 int。
    """
    if msg_id is None:
        return None
    try:
        from telethon.tl.types import InputReplyToMessage
        return InputReplyToMessage(reply_to_msg_id=msg_id)
    except ImportError:
        return msg_id


def _get_reply_to_msg_id(message) -> Optional[int]:
    """兼容新旧版 Telethon 获取 reply_to_msg_id。"""
    if hasattr(message, 'reply_to_msg_id') and message.reply_to_msg_id is not None:
        return message.reply_to_msg_id
    if hasattr(message, 'reply_to') and message.reply_to is not None:
        if hasattr(message.reply_to, 'reply_to_msg_id'):
            return message.reply_to.reply_to_msg_id
    return None


def _get_reply_to_top_id(message) -> Optional[int]:
    reply_to = getattr(message, 'reply_to', None)
    if reply_to is not None:
        top_id = getattr(reply_to, 'reply_to_top_id', None)
        if top_id is not None:
            return top_id
        msg_id = getattr(reply_to, 'reply_to_msg_id', None)
        if msg_id is not None:
            return msg_id
    if hasattr(message, 'reply_to_msg_id') and message.reply_to_msg_id is not None:
        return message.reply_to_msg_id
    return None


async def get_discussion_message(
    client: TelegramClient,
    channel_id: Union[int, str],
    post_id: int,
) -> Optional[Message]:
    try:
        result = await client(GetDiscussionMessageRequest(peer=channel_id, msg_id=post_id))
    except Exception as e:
        logging.warning(f"⚠️ 获取讨论消息失败: {e}")
        return None
    messages = getattr(result, 'messages', None) or []
    if not messages:
        return None

    channel_ids = set()
    if isinstance(channel_id, int):
        channel_ids.add(channel_id)
        if channel_id > 0:
            channel_ids.add(int(f"-100{channel_id}"))
        else:
            # 修复 #8: channel_id 为负数时，提取裸 ID
            bare_str = str(channel_id).lstrip('-')
            if bare_str.startswith("100") and len(bare_str) > 3:
                bare_id = int(bare_str[3:])
                channel_ids.add(bare_id)
    else:
        try:
            entity = await client.get_entity(channel_id)
            resolved = getattr(entity, 'id', None)
            if resolved is not None:
                channel_ids.add(resolved)
                if resolved > 0:
                    channel_ids.add(int(f"-100{resolved}"))
        except Exception:
            pass

    for msg in messages:
        msg_chat_id = getattr(msg, 'chat_id', None)
        if msg_chat_id is not None and msg_chat_id not in channel_ids:
            return msg
    return messages[0]


async def get_discussion_group_id(client: TelegramClient, channel_id: Union[int, str]) -> Optional[int]:
    try:
        entity = await client.get_entity(channel_id)
        result = await client(GetFullChannelRequest(channel=entity))
        linked_id = getattr(result.full_chat, 'linked_chat_id', None)
        return linked_id
    except Exception as e:
        logging.warning(f"⚠️ 获取讨论组失败: {e}")
        return None


def _extract_comment_keyword(text: str, forward=None) -> Optional[str]:
    if not text:
        return None
    prefixes_raw = getattr(forward, 'comment_keyword_prefixes_raw', '') if forward else ''
    suffixes_raw = getattr(forward, 'comment_keyword_suffixes_raw', '') if forward else ''
    prefixes = [s.strip() for s in (prefixes_raw or CONFIG.bot_media.comment_keyword_prefixes_raw).splitlines() if s.strip()]
    suffixes = [s.strip() for s in (suffixes_raw or CONFIG.bot_media.comment_keyword_suffixes_raw).splitlines() if s.strip()]
    for prefix in prefixes:
        idx = text.find(prefix)
        if idx == -1:
            continue
        remainder = text[idx + len(prefix):]
        if suffixes:
            end_idx = None
            for suffix in suffixes:
                pos = remainder.find(suffix)
                if pos != -1 and (end_idx is None or pos < end_idx):
                    end_idx = pos
            if end_idx is not None:
                keyword = remainder[:end_idx].strip()
                if keyword:
                    return keyword
        keyword = remainder.strip()
        if keyword:
            return keyword
    return None


async def _auto_comment_keyword(client: TelegramClient, channel_id: Union[int, str], post_id: int, keyword: str) -> None:
    if not keyword:
        return
    disc_msg = await get_discussion_message(client, channel_id, post_id)
    if disc_msg is None:
        return
    try:
        await client.send_message(disc_msg.chat_id, keyword, reply_to=disc_msg.id)
    except Exception as e:
        logging.warning(f"⚠️ 自动评论失败: {e}")


async def resolve_bot_media_from_message(client: TelegramClient, message: Message, forward=None) -> List[Message]:
    if message is None:
        return []
    if not CONFIG.bot_media.enabled:
        return []
    if forward is not None and forward.bot_media_enabled is False:
        return []
    blacklist_raw = getattr(forward, 'bot_media_tme_link_blacklist_raw', '') if forward else ''
    blacklist = [s.strip() for s in (blacklist_raw or CONFIG.bot_media.tme_link_blacklist_raw).splitlines() if s.strip()]

    def is_blacklisted(url: str) -> bool:
        return any(b in url for b in blacklist)

    def collect_links(text: str) -> List[str]:
        if not text:
            return []
        return re.findall(r"(https?://t\.me/[^\s]+)", text)

    links = set(collect_links(message.raw_text or message.text or ""))
    markup = getattr(message, 'reply_markup', None)
    if isinstance(markup, ReplyInlineMarkup):
        for row in markup.rows or []:
            for btn in row.buttons or []:
                if isinstance(btn, KeyboardButtonUrl):
                    links.add(btn.url)
                elif isinstance(btn, KeyboardButtonCallback):
                    continue

    targets = {}
    user_targets = {}
    for url in links:
        if is_blacklisted(url):
            continue
        m = re.search(r"(?:https?://)?t\.me/c/(\d+)/(\d+)", url)
        if m:
            chat_id = int(f"-100{m.group(1)}")
            msg_id = int(m.group(2))
            targets.setdefault(chat_id, set()).add(msg_id)
            continue
        m = re.search(r"(?:https?://)?t\.me/([A-Za-z0-9_]+)/(\d+)", url)
        if m:
            username = m.group(1)
            msg_id = int(m.group(2))
            user_targets.setdefault(username, set()).add(msg_id)

    results: List[Message] = []
    seen = set()
    for chat_id, ids in targets.items():
        msgs = await client.get_messages(chat_id, ids=list(ids))
        if msgs is None:
            continue
        if isinstance(msgs, Message):
            msgs = [msgs]
        for msg in msgs:
            if msg and msg.media:
                key = (msg.chat_id, msg.id)
                if key not in seen:
                    seen.add(key)
                    results.append(msg)
    for username, ids in user_targets.items():
        try:
            entity = await client.get_entity(username)
        except Exception:
            continue
        msgs = await client.get_messages(entity, ids=list(ids))
        if msgs is None:
            continue
        if isinstance(msgs, Message):
            msgs = [msgs]
        for msg in msgs:
            if msg and msg.media:
                key = (msg.chat_id, msg.id)
                if key not in seen:
                    seen.add(key)
                    results.append(msg)
    return results


# =====================================================================
#  Spoiler 检测与发送
# =====================================================================

def _has_spoiler(message: Message) -> bool:
    if not message or not message.media:
        return False
    return getattr(message.media, 'spoiler', False)


def _extract_message_from_updates(result) -> Optional[Message]:
    """从 Updates 结果中安全提取第一条 Message，找不到返回 None。"""
    if isinstance(result, Message):
        return result
    if hasattr(result, 'updates'):
        for update in result.updates:
            if hasattr(update, 'message') and update.message is not None:
                return update.message
    return None


def _extract_messages_from_updates(result) -> List:
    """从 Updates 结果中安全提取所有 Message。"""
    if isinstance(result, list):
        return result
    messages = []
    if hasattr(result, 'updates'):
        for update in result.updates:
            if hasattr(update, 'message') and update.message is not None:
                messages.append(update.message)
    return messages


async def _send_single_with_spoiler(
    client: TelegramClient,
    recipient: EntityLike,
    message: Message,
    caption: Optional[str] = None,
    reply_to: Optional[int] = None,
) -> Optional[Message]:
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

    # 修复 #6: 使用 _build_reply_to 兼容新旧版 Telethon
    reply_to_obj = _build_reply_to(reply_to)

    send_kwargs = {
        'peer': peer,
        'media': input_media,
        'message': caption or '',
        'random_id': random.randrange(-2**63, 2**63),
    }
    if reply_to_obj is not None:
        # 新版 Telethon 使用 reply_to 参数
        try:
            from telethon.tl.types import InputReplyToMessage
            send_kwargs['reply_to'] = reply_to_obj
        except ImportError:
            send_kwargs['reply_to_msg_id'] = reply_to

    result = await client(SendMediaRequest(**send_kwargs))

    # 修复 #4: 安全提取 Message
    extracted = _extract_message_from_updates(result)
    if extracted is not None:
        return extracted
    logging.warning("⚠️ 无法从 Updates 中提取 Message，返回 None")
    return None


async def _send_album_with_spoiler(
    client: TelegramClient,
    recipient: EntityLike,
    grouped_messages: List[Message],
    caption: Optional[str] = None,
    reply_to: Optional[int] = None,
) -> List:
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

    # 修复 #6: 使用 _build_reply_to 兼容新旧版 Telethon
    if reply_to is not None:
        try:
            from telethon.tl.types import InputReplyToMessage
            kwargs['reply_to'] = _build_reply_to(reply_to)
        except ImportError:
            kwargs['reply_to_msg_id'] = reply_to

    result = await client(SendMultiMediaRequest(**kwargs))

    # 修复 #5: 安全提取消息列表
    sent_messages = _extract_messages_from_updates(result)

    logging.info(f"✅ 发送媒体组完成 ({len(multi_media)} 项)")
    if sent_messages:
        return sent_messages
    logging.warning("⚠️ 无法从 Updates 中提取消息列表")
    return []


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
    """发送消息的统一入口。"""
    client: TelegramClient = tm.client
    reply_to_target = comment_to_post if comment_to_post is not None else tm.reply_to

    # === 情况 1: 直接转发（保留 forwarded from） ===
    if comment_to_post is None and CONFIG.show_forwarded_from and grouped_messages:
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
                    attempt += 1
                    continue  # 修复 #1: 跳过下面的二次 sleep
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
                        reply_to=reply_to_target,
                    )
                else:
                    # 修复 #2: 提取媒体对象而非 Message 对象，caption 用列表
                    media_to_send = []
                    for msg in grouped_messages:
                        if msg.photo:
                            media_to_send.append(msg.photo)
                        elif msg.video:
                            media_to_send.append(msg.video)
                        elif msg.gif:
                            media_to_send.append(msg.gif)
                        elif msg.document:
                            media_to_send.append(msg.document)

                    if not media_to_send:
                        return await client.send_message(
                            recipient,
                            combined_caption or "空相册",
                            reply_to=reply_to_target,
                        )

                    # 修复 #2: caption 构造为列表，第一个文件带 caption，其余为空
                    caption_list = [''] * len(media_to_send)
                    if combined_caption:
                        caption_list[0] = combined_caption

                    result = await client.send_file(
                        recipient, media_to_send,
                        caption=caption_list,
                        reply_to=reply_to_target,
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
                    attempt += 1
                    continue  # 修复 #1: 跳过下面的二次 sleep
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
                reply_to=reply_to_target,
                supports_streaming=True,
                buttons=processed_markup,
            )
        except Exception as e:
            logging.warning(f"⚠️ 带按钮发送新文件失败，去除按钮重试: {e}")
            try:
                return await client.send_file(
                    recipient, tm.new_file,
                    caption=tm.text,
                    reply_to=reply_to_target,
                    supports_streaming=True,
                )
            except Exception as e2:
                logging.error(f"❌ 新文件发送最终失败: {e2}")
                return None

    # 3b: 单条带 spoiler 的媒体
    if tm.message and _has_spoiler(tm.message):
        logging.info("🔒 单条 Spoiler 消息，使用底层 API")
        try:
            result = await _send_single_with_spoiler(
                client, recipient, tm.message,
                caption=tm.text, reply_to=reply_to_target,
            )
            logging.info("✅ 带 spoiler 单条消息发送成功")
            return result
        except Exception as e:
            logging.warning(f"⚠️ spoiler 发送失败，回退普通模式: {e}")

    # 3c: 普通消息 —— ★ 正确处理 reply_markup ★
    try:
        msg_media = tm.message.media if tm.message else None

        if processed_markup is not None:
            # 有处理后的按钮，用分离参数发送（避免原始 Message 对象携带旧 markup）
            try:
                return await client.send_message(
                    recipient,
                    tm.text,
                    file=msg_media if msg_media else None,
                    buttons=processed_markup,
                    reply_to=reply_to_target,
                    link_preview=not bool(msg_media),
                )
            except Exception as e:
                logging.warning(f"⚠️ 带按钮发送失败，去除按钮重试: {e}")
                # 回退：不带按钮
                if msg_media:
                    return await client.send_message(
                        recipient,
                        tm.text,
                        file=msg_media,
                        reply_to=reply_to_target,
                        link_preview=False,
                    )
                else:
                    return await client.send_message(
                        recipient,
                        tm.text,
                        reply_to=reply_to_target,
                    )
        else:
            # markup 为 None —— 必须避免把原始 Message 的按钮带过去
            if msg_media:
                return await client.send_message(
                    recipient,
                    tm.text,
                    file=msg_media,
                    reply_to=reply_to_target,
                    link_preview=False,
                )
            else:
                return await client.send_message(
                    recipient,
                    tm.text,
                    reply_to=reply_to_target,
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
