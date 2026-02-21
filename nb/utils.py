# nb/utils.py

import logging
import asyncio
import re
from urllib.parse import urlparse, parse_qs
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
    InputReplyToMessage,
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
    GetBotCallbackAnswerRequest,
)

from nb import __version__
from nb.config import CONFIG
from nb.plugin_models import STYLE_CODES

if TYPE_CHECKING:
    from nb.plugins import NbMessage


MAX_RETRIES = 5
RETRY_BASE_DELAY = 5


# =====================================================================
#  reply_to 兼容辅助（兼容 1.34+）
# =====================================================================

def _get_reply_to_msg_id(message) -> Optional[int]:
    """兼容所有版本获取 reply_to_msg_id。"""
    if hasattr(message, 'reply_to') and message.reply_to is not None:
        if hasattr(message.reply_to, 'reply_to_msg_id'):
            return message.reply_to.reply_to_msg_id
    if hasattr(message, 'reply_to_msg_id') and message.reply_to_msg_id is not None:
        return message.reply_to_msg_id
    return None


def _get_reply_to_top_id(message) -> Optional[int]:
    """获取评论所属的顶层帖子 ID。"""
    reply_to = getattr(message, 'reply_to', None)
    if reply_to is None:
        return None
    return getattr(reply_to, 'reply_to_top_id', None)


def _make_reply_to(msg_id: Optional[int]):
    """
    构造 reply_to 参数
    Telethon 1.34+ 底层 API 要求使用 InputReplyToMessage
    """
    if msg_id is None:
        return None
    try:
        return InputReplyToMessage(reply_to_msg_id=msg_id)
    except Exception:
        return msg_id


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


def _extract_tme_links(text: str) -> List[str]:
    if not text:
        return []
    candidates = re.findall(r"(https?://t\.me/[^\s]+|t\.me/[^\s]+)", text)
    return [c.strip(").,;\"'") for c in candidates]


# ✅ 新增：从消息 entities 中提取 t.me 链接（处理超链接实体）
def _extract_tme_links_from_entities(message: Message) -> List[str]:
    """从消息的 entities 中提取 t.me 链接（处理超链接实体）"""
    if not message:
        return []
    entities = getattr(message, 'entities', None) or []
    links = []
    text = message.raw_text or message.text or ""
    for ent in entities:
        # TextUrl: 显示文字与实际 URL 不同（如 "点击获取" 实际链接是 t.me/xxx）
        if hasattr(ent, 'url') and ent.url:
            url = ent.url
            if 't.me/' in url:
                links.append(url.strip())
        # Url: 文本本身就是 URL（兜底，_extract_tme_links 通常已覆盖）
        elif hasattr(ent, 'offset') and hasattr(ent, 'length'):
            type_name = type(ent).__name__
            if 'Url' in type_name:
                url_text = text[ent.offset:ent.offset + ent.length]
                if 't.me/' in url_text:
                    links.append(url_text.strip())
    return links


def _parse_tme_start_link(url: str) -> Optional[tuple]:
    if not url:
        return None
    if url.startswith("t.me/"):
        url = "https://" + url
    try:
        parsed = urlparse(url)
    except Exception:
        return None
    path = parsed.path.lstrip("/")
    if not path:
        return None
    bot_username = path.split("/")[0]
    qs = parse_qs(parsed.query or "")
    start_param = qs.get("start", [None])[0]
    if not start_param:
        return None
    return bot_username, start_param


def _extract_start_links_from_markup(reply_markup) -> List[tuple]:
    if reply_markup is None or not isinstance(reply_markup, ReplyInlineMarkup):
        return []
    found = []
    for row in reply_markup.rows:
        for button in row.buttons:
            if isinstance(button, KeyboardButtonUrl):
                url = button.url or ""
                parsed = _parse_tme_start_link(url)
                if parsed:
                    found.append(parsed)
    return found


def _extract_bot_usernames(text: str) -> List[str]:
    if not text:
        return []
    matches = re.findall(r"@([A-Za-z0-9_]{5,})", text)
    bots = []
    for name in matches:
        if name.lower().endswith("bot"):
            bots.append(name)
    return bots


def _parse_lines(raw: str) -> List[str]:
    if not raw:
        return []
    lines = [line.strip() for line in raw.replace("\r", "\n").split("\n")]
    return [line for line in lines if line]


def _trim_keyword(value: str) -> str:
    if not value:
        return value
    return value.strip().strip(" \"'""''()（）[]【】{}<>《》")


def _get_bot_media_value(forward, name: str, default: str = "") -> str:
    if forward is not None:
        value = getattr(forward, name, "")
        if isinstance(value, str) and value.strip():
            return value
    value = getattr(CONFIG.bot_media, name, default)
    return value if isinstance(value, str) else default


def _extract_comment_keyword(text: str, forward=None) -> Optional[str]:
    if not text:
        return None
    prefixes = _parse_lines(_get_bot_media_value(forward, "comment_keyword_prefixes_raw"))
    suffixes = _parse_lines(_get_bot_media_value(forward, "comment_keyword_suffixes_raw"))
    if not prefixes or not suffixes:
        return None
    for prefix in prefixes:
        start = text.find(prefix)
        if start == -1:
            continue
        start_idx = start + len(prefix)
        end_candidates = []
        for suffix in suffixes:
            end = text.find(suffix, start_idx)
            if end != -1:
                end_candidates.append(end)
        if not end_candidates:
            continue
        end_idx = min(end_candidates)
        keyword = text[start_idx:end_idx]
        keyword = _trim_keyword(keyword)
        if keyword:
            return keyword
    return None


def _find_next_callback_button(reply_markup, forward=None) -> Optional[KeyboardButtonCallback]:
    if reply_markup is None or not isinstance(reply_markup, ReplyInlineMarkup):
        return None
    mode = _get_bot_media_value(forward, "bot_media_pagination_mode", "")
    if not mode:
        mode = getattr(CONFIG.bot_media, "pagination_mode", "auto")
    next_keywords = [
        "next", "more", "next page", "nextpage", "continue", "remaining", "send remaining",
        "下一页", "下页", "继续", "更多", "继续发送", "发送剩余", "剩余", "查看更多", "下一个", "翻页", "➡", ">",
    ]
    get_all_keywords = [
        "get all", "getall", "all", "all files", "fetch all", "download all",
        "获取全部", "全部获取", "一键获取", "获取所有", "查看全部", "全部发送", "一键发送",
    ]
    custom_keywords = _parse_lines(_get_bot_media_value(forward, "bot_media_pagination_keywords_raw"))
    if not custom_keywords:
        custom_keywords = _parse_lines(CONFIG.bot_media.pagination_keywords_raw)
    if mode == "any":
        keywords = []
    else:
        keywords = next_keywords + get_all_keywords + custom_keywords
    for row in reply_markup.rows:
        for button in row.buttons:
            if isinstance(button, KeyboardButtonCallback):
                if mode == "any":
                    return button
                text = (button.text or "").strip().lower()
                compact = text.replace(" ", "")
                if any(k in text or k in compact for k in keywords):
                    return button
    return None


async def _auto_comment_keyword(
    client: TelegramClient,
    channel_id: Union[int, str],
    post_id: int,
    keyword: str,
) -> bool:
    if CONFIG.login.user_type == 0:
        return False
    disc_msg = await get_discussion_message(client, channel_id, post_id)
    if disc_msg is None:
        return False
    try:
        await client.send_message(disc_msg.chat_id, keyword, reply_to=disc_msg.id)
        return True
    except Exception as e:
        logging.warning(f"⚠️ 评论区触发失败: {e}")
        return False


async def _collect_new_messages(
    client: TelegramClient,
    peer,
    min_id: int,
    timeout: float,
) -> List[Message]:
    start = asyncio.get_running_loop().time()
    seen = set()
    collected: List[Message] = []
    while True:
        new_found = False
        async for msg in client.iter_messages(peer, min_id=min_id, reverse=True):
            if msg.id in seen:
                continue
            seen.add(msg.id)
            collected.append(msg)
            new_found = True
        if collected and not new_found:
            break
        if asyncio.get_running_loop().time() - start >= timeout:
            break
        await asyncio.sleep(CONFIG.bot_media.poll_interval)
    return collected


async def _get_grouped_messages_from_bot(
    client: TelegramClient,
    bot,
    grouped_id: int,
) -> List[Message]:
    result = []
    async for msg in client.iter_messages(bot, limit=CONFIG.bot_media.recent_limit):
        if msg.grouped_id == grouped_id:
            result.append(msg)
    result.sort(key=lambda m: m.id)
    return result


async def _start_bot_and_collect_album(
    client: TelegramClient,
    bot_username: str,
    start_param: str,
    max_pages: Optional[int] = None,
    wait_timeout: Optional[float] = None,
    forward=None,
) -> List[Message]:
    if max_pages is None:
        max_pages = CONFIG.bot_media.max_pages
    if not CONFIG.bot_media.enable_pagination:
        max_pages = 0
    if wait_timeout is None:
        wait_timeout = CONFIG.bot_media.wait_timeout
    bot = await client.get_entity(bot_username)
    latest = await client.get_messages(bot, limit=1)
    last_id = latest[0].id if latest else 0
    await client.send_message(bot, f"/start {start_param}")
    collected: List[Message] = []
    seen_grouped = set()
    seen_ids = set()
    pages = 0
    while pages <= max_pages:
        new_msgs = await _collect_new_messages(client, bot, last_id, wait_timeout)
        if not new_msgs:
            break
        last_id = max(m.id for m in new_msgs)
        for msg in new_msgs:
            if msg.grouped_id:
                if msg.grouped_id in seen_grouped:
                    continue
                grouped = await _get_grouped_messages_from_bot(client, bot, msg.grouped_id)
                if grouped:
                    for gmsg in grouped:
                        if gmsg.media and gmsg.id not in seen_ids:
                            collected.append(gmsg)
                            seen_ids.add(gmsg.id)
                seen_grouped.add(msg.grouped_id)
            else:
                if msg.media and msg.id not in seen_ids:
                    collected.append(msg)
                    seen_ids.add(msg.id)
        next_btn = None
        next_msg = None
        for msg in reversed(new_msgs):
            next_btn = _find_next_callback_button(msg.reply_markup, forward)
            if next_btn:
                next_msg = msg
                break
        if next_btn and next_msg:
            try:
                await client(GetBotCallbackAnswerRequest(peer=bot, msg_id=next_msg.id, data=next_btn.data))
                pages += 1
                continue
            except Exception:
                break
        break
    collected.sort(key=lambda m: m.id)
    return collected


async def resolve_bot_media_from_message(
    client: TelegramClient,
    message: Message,
    forward=None,
) -> List[Message]:
    if not CONFIG.bot_media.enabled:
        return []
    if CONFIG.login.user_type == 0:
        logging.warning("⚠️ bot 媒体拉取需要 user 模式")
        return []

    # ✅ 修复：同时从纯文本和 entities 中提取链接
    text_links = _extract_tme_links(message.raw_text or message.text or "")
    entity_links = _extract_tme_links_from_entities(message)
    all_links = list(dict.fromkeys(text_links + entity_links))  # 去重保序

    found = []
    for link in all_links:
        parsed = _parse_tme_start_link(link)
        if parsed:
            found.append(parsed)
    found.extend(_extract_start_links_from_markup(message.reply_markup))

    collected: List[Message] = []
    for bot_username, start_param in found:
        try:
            items = await _start_bot_and_collect_album(client, bot_username, start_param, forward=forward)
            if items:
                collected.extend(items)
        except Exception as e:
            logging.warning(f"⚠️ bot 媒体拉取失败 ({bot_username}): {e}")
    if collected:
        return collected
    keyword_trigger_enabled = CONFIG.bot_media.enable_keyword_trigger
    if forward is not None and forward.bot_media_keyword_trigger_enabled is not None:
        keyword_trigger_enabled = forward.bot_media_keyword_trigger_enabled
    if not keyword_trigger_enabled:
        return []
    bot_names = _extract_bot_usernames(message.raw_text or message.text or "")
    if not bot_names:
        return []
    keyword = (message.raw_text or message.text or "").strip()
    if not keyword:
        return []
    for bot_username in bot_names[:1]:
        try:
            bot = await client.get_entity(bot_username)
            latest = await client.get_messages(bot, limit=1)
            last_id = latest[0].id if latest else 0
            await client.send_message(bot, keyword)
            responses = await _collect_new_messages(client, bot, last_id, CONFIG.bot_media.wait_timeout)
            for msg in responses:
                new_links = _extract_tme_links(msg.raw_text or msg.text or "")
                for link in new_links:
                    parsed = _parse_tme_start_link(link)
                    if parsed:
                        items = await _start_bot_and_collect_album(client, parsed[0], parsed[1], forward=forward)
                        if items:
                            collected.extend(items)
            if collected:
                break
        except Exception as e:
            logging.warning(f"⚠️ bot 关键字请求失败 ({bot_username}): {e}")
    return collected


def _is_flood_wait(e: Exception) -> bool:
    return "FLOOD_WAIT" in str(e).upper() or "flood" in str(e).lower()


async def _handle_flood_wait(e: Exception) -> int:
    wait_match = re.search(r'(\d+)', str(e))
    wait_sec = int(wait_match.group()) if wait_match else 30
    logging.critical(f"⛔ FloodWait: 等待 {wait_sec + 10} 秒")
    await asyncio.sleep(wait_sec + 10)
    return wait_sec


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

    kwargs = {
        'peer': peer,
        'media': input_media,
        'message': caption or '',
        'random_id': random.randrange(-2**63, 2**63),
    }
    if reply_to is not None:
        kwargs['reply_to'] = _make_reply_to(reply_to)

    result = await client(SendMediaRequest(**kwargs))

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
        kwargs['reply_to'] = _make_reply_to(reply_to)

    result = await client(SendMultiMediaRequest(**kwargs))

    sent_messages = []
    if hasattr(result, 'updates'):
        for update in result.updates:
            if hasattr(update, 'message'):
                sent_messages.append(update.message)

    return sent_messages if sent_messages else result


def _get_download_client(tm: "NbMessage") -> TelegramClient:
    msg_client = getattr(tm.message, '_client', None) or getattr(tm.message, 'client', None)
    if msg_client is not None:
        return msg_client
    return tm.client


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
    client: TelegramClient = tm.client
    effective_reply_to = comment_to_post if comment_to_post else tm.reply_to

    # 1. 转发消息 (Show Forwarded From)
    if CONFIG.show_forwarded_from:
        if grouped_messages:
            attempt = 0
            delay = 5
            while attempt < MAX_RETRIES:
                try:
                    result = await client.forward_messages(recipient, grouped_messages)
                    logging.info(f"✅ 直接转发媒体组成功")
                    return result
                except Exception as e:
                    if _is_flood_wait(e): await _handle_flood_wait(e)
                    else: logging.error(f"❌ 转发失败: {e}")
                    attempt += 1
                    await asyncio.sleep(delay)
            return None
        else:
            attempt = 0
            delay = 5
            while attempt < MAX_RETRIES:
                try:
                    result = await client.forward_messages(
                        recipient, tm.message.id, from_peer=tm.message.chat_id,
                    )
                    if isinstance(result, list): result = result[0] if result else None
                    logging.info(f"✅ forward 成功 msg={tm.message.id}")
                    return result
                except Exception as e:
                    if _is_flood_wait(e): await _handle_flood_wait(e)
                    else: logging.error(f"❌ forward 失败: {e}")
                    attempt += 1
                    await asyncio.sleep(delay)
            return None

    # 2. 媒体组发送 (Send Album)
    if grouped_messages and grouped_tms:
        combined_caption = "\n\n".join([gtm.text.strip() for gtm in grouped_tms if gtm.text and gtm.text.strip()])
        any_spoiler = any(_has_spoiler(msg) for msg in grouped_messages)
        attempt = 0
        while attempt < MAX_RETRIES:
            try:
                if any_spoiler:
                    result = await _send_album_with_spoiler(
                        client, recipient, grouped_messages,
                        caption=combined_caption or None,
                        reply_to=effective_reply_to,
                    )
                else:
                    files_to_send = [msg for msg in grouped_messages if msg.photo or msg.video or msg.gif or msg.document]
                    if not files_to_send:
                        return await client.send_message(recipient, combined_caption or "空相册", reply_to=effective_reply_to)
                    result = await client.send_file(
                        recipient, files_to_send,
                        caption=combined_caption or None,
                        reply_to=effective_reply_to,
                        supports_streaming=True,
                        force_document=False,
                        allow_cache=False,
                        parse_mode="md",
                    )
                logging.info(f"✅ 媒体组发送成功")
                return result
            except Exception as e:
                if _is_flood_wait(e): await _handle_flood_wait(e)
                else: logging.error(f"❌ 媒体组发送失败: {e}")
                attempt += 1
                await asyncio.sleep(5)
        return None

    # 3. 单条消息发送
    processed_markup = getattr(tm, 'reply_markup', None)
    
    # 3a. 插件生成的新文件
    if tm.new_file:
        try:
            return await client.send_file(
                recipient, tm.new_file,
                caption=tm.text,
                reply_to=effective_reply_to,
                supports_streaming=True,
                buttons=processed_markup,
            )
        except Exception as e:
            logging.warning(f"⚠️ 新文件发送失败 (重试无按钮): {e}")
            try:
                return await client.send_file(
                    recipient, tm.new_file,
                    caption=tm.text,
                    reply_to=effective_reply_to,
                    supports_streaming=True,
                )
            except Exception as e2:
                logging.error(f"❌ 新文件发送最终失败: {e2}")
                return None

    # 3b. Spoiler 媒体
    if _has_spoiler(tm.message):
        try:
            result = await _send_single_with_spoiler(
                client, recipient, tm.message,
                caption=tm.text, reply_to=effective_reply_to,
            )
            logging.info("✅ Spoiler 消息发送成功")
            return result
        except Exception as e:
            logging.warning(f"⚠️ spoiler 发送失败，回退: {e}")

    # 3c. 普通消息
    attempt = 0
    while attempt < MAX_RETRIES:
        try:
            tm.message.text = tm.text
            if processed_markup is not None:
                try:
                    result = await client.send_message(
                        recipient, tm.message,
                        reply_to=effective_reply_to,
                        buttons=processed_markup,
                    )
                    logging.info(f"✅ copy 成功(带按钮) msg={tm.message.id}")
                    return result
                except Exception as e_btn:
                    logging.warning(f"⚠️ 带按钮失败: {e_btn}")

            result = await client.send_message(
                recipient, tm.message,
                reply_to=effective_reply_to,
            )
            logging.info(f"✅ copy 成功 msg={tm.message.id}")
            return result
        except Exception as e:
            if _is_flood_wait(e): await _handle_flood_wait(e)
            else: logging.error(f"❌ copy 失败: {e}")
            attempt += 1
            await asyncio.sleep(5)
    return None


def cleanup(*files: str) -> None:
    for file in files:
        try:
            os.remove(file)
        except FileNotFoundError:
            pass


def stamp(file: str, user: str) -> str:
    now = str(datetime.now())
    outf = safe_name(f"{user} {now} {file}")
    try:
        os.rename(file, outf)
        return outf
    except Exception as err:
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
