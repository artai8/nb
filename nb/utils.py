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
    MessageMediaWebPage,
    MessageMediaGame,
    MessageMediaPoll,
    MessageMediaDice,
    MessageMediaContact,
    MessageMediaGeo,
    MessageMediaVenue,
)
from telethon.tl.functions.messages import (
    SendMediaRequest,
    SendMultiMediaRequest,
    GetDiscussionMessageRequest,
)
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.errors.rpcerrorlist import (
    MediaInvalidError,
    MediaEmptyError,
    FileReferenceExpiredError,
    FileReferenceInvalidError,
)

from nb import __version__
from nb.config import CONFIG
from nb.plugin_models import STYLE_CODES

if TYPE_CHECKING:
    from nb.plugins import NbMessage


MAX_RETRIES = 3


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
    for attempt in range(3):
        try:
            result = await client(GetDiscussionMessageRequest(
                peer=channel_id,
                msg_id=msg_id,
            ))
            if result and result.messages:
                disc_msg = result.messages[0]
                logging.info(
                    f"💬 获取讨论消息成功: channel({channel_id}, post={msg_id}) "
                    f"→ discussion(chat={disc_msg.chat_id}, msg={disc_msg.id})"
                )
                return disc_msg
            else:
                return None
        except Exception as e:
            err_str = str(e).upper()
            if "FLOOD" in err_str:
                wait = 10 * (attempt + 1)
                logging.warning(f"⛔ FloodWait 获取讨论消息，等待 {wait}s")
                await asyncio.sleep(wait)
                continue
            if "MSG_ID_INVALID" in err_str:
                return None
            if "CHANNEL_PRIVATE" in err_str or "CHAT_ADMIN_REQUIRED" in err_str:
                return None
            if "DISCUSSION" in err_str and "DISABLED" in err_str:
                logging.debug(f"频道 {channel_id} 未开启评论功能")
                return None
            logging.warning(
                f"⚠️ 获取讨论消息失败 (attempt {attempt+1}/3, "
                f"channel={channel_id}, msg={msg_id}): {e}"
            )
            if attempt < 2:
                await asyncio.sleep(2)
    return None


async def get_discussion_group_id(
    client: TelegramClient,
    channel_id: Union[int, str],
) -> Optional[int]:
    try:
        input_channel = await client.get_input_entity(channel_id)
        full_result = await client(GetFullChannelRequest(input_channel))
        full_chat = full_result.full_chat
        linked_chat_id = getattr(full_chat, 'linked_chat_id', None)
        if linked_chat_id:
            logging.info(f"💬 频道 {channel_id} 的讨论组: {linked_chat_id}")
            return linked_chat_id
        else:
            logging.warning(f"⚠️ 频道 {channel_id} 没有关联讨论组")
            return None
    except Exception as e:
        logging.warning(f"⚠️ 获取讨论组失败 (channel={channel_id}): {e}")
    return None


def _has_spoiler(message: Message) -> bool:
    if not message or not message.media:
        return False
    return getattr(message.media, 'spoiler', False)


def _is_unsupported_media(message: Message) -> bool:
    """★ 新增：检测不支持转发的媒体类型"""
    if not message or not message.media:
        return False
    
    media = message.media
    
    # 游戏消息：用户账号无法发送
    if isinstance(media, MessageMediaGame):
        return True
    
    # 投票消息：需要特殊处理
    if isinstance(media, MessageMediaPoll):
        return True
    
    # 骰子消息
    if isinstance(media, MessageMediaDice):
        return True
    
    return False


def _get_media_type_name(message: Message) -> str:
    """获取媒体类型名称用于日志"""
    if not message or not message.media:
        return "text"
    
    media = message.media
    if isinstance(media, MessageMediaPhoto):
        return "photo"
    if isinstance(media, MessageMediaDocument):
        doc = media.document
        if doc:
            for attr in doc.attributes:
                if isinstance(attr, DocumentAttributeVideo):
                    return "video"
                if isinstance(attr, DocumentAttributeAnimated):
                    return "gif"
                if isinstance(attr, DocumentAttributeAudio):
                    return "audio"
                if isinstance(attr, DocumentAttributeSticker):
                    return "sticker"
        return "document"
    if isinstance(media, MessageMediaWebPage):
        return "webpage"
    if isinstance(media, MessageMediaGame):
        return "game"
    if isinstance(media, MessageMediaPoll):
        return "poll"
    if isinstance(media, MessageMediaDice):
        return "dice"
    if isinstance(media, MessageMediaContact):
        return "contact"
    if isinstance(media, (MessageMediaGeo, MessageMediaVenue)):
        return "location"
    
    return "unknown"


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

    return sent_messages if sent_messages else result


async def _download_and_resend(
    client: TelegramClient,
    recipient: EntityLike,
    message: Message,
    caption: Optional[str] = None,
    reply_to: Optional[int] = None,
) -> Optional[Message]:
    """★ 新增：下载媒体后重新发送（用于处理文件引用过期等问题）"""
    try:
        # 下载媒体到临时文件
        file_path = await message.download_media()
        if not file_path:
            logging.warning("⚠️ 无法下载媒体文件")
            return None
        
        try:
            # 重新发送
            result = await client.send_file(
                recipient,
                file_path,
                caption=caption or "",
                reply_to=reply_to,
                supports_streaming=True,
            )
            return result
        finally:
            # 清理临时文件
            try:
                os.remove(file_path)
            except Exception:
                pass
    except Exception as e:
        logging.error(f"❌ 下载重发失败: {e}")
        return None


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
    """★ 增强：支持多重回退机制的消息发送"""
    client: TelegramClient = tm.client
    effective_reply_to = comment_to_post if comment_to_post else tm.reply_to
    
    # ★ 检测不支持的媒体类型
    if _is_unsupported_media(tm.message):
        media_type = _get_media_type_name(tm.message)
        logging.warning(f"⚠️ 跳过不支持的媒体类型: {media_type}")
        
        # 如果有文字，只发送文字
        if tm.text and tm.text.strip():
            try:
                return await client.send_message(
                    recipient,
                    f"[原消息包含 {media_type}，无法转发]\n\n{tm.text}",
                    reply_to=effective_reply_to,
                )
            except Exception as e:
                logging.error(f"❌ 发送替代文本失败: {e}")
        return None

    # ========== 转发模式（show_forwarded_from=True）==========
    if CONFIG.show_forwarded_from and grouped_messages:
        attempt = 0
        delay = 5
        while attempt < MAX_RETRIES:
            try:
                result = await client.forward_messages(recipient, grouped_messages)
                return result
            except Exception as e:
                if "FLOOD_WAIT" in str(e).upper():
                    wait_match = re.search(r'\d+', str(e))
                    wait_sec = int(wait_match.group()) if wait_match else 30
                    await asyncio.sleep(wait_sec + 10)
                else:
                    logging.error(f"❌ 转发失败 ({attempt+1}/{MAX_RETRIES}): {e}")
                attempt += 1
                delay = min(delay * 2, 300)
                await asyncio.sleep(delay)
        return None

    # ========== 媒体组发送 ==========
    if grouped_messages and grouped_tms:
        combined_caption = "\n\n".join([
            gtm.text.strip() for gtm in grouped_tms
            if gtm.text and gtm.text.strip()
        ])

        any_spoiler = any(_has_spoiler(msg) for msg in grouped_messages)
        
        # ★ 过滤掉不支持的媒体
        valid_messages = [
            msg for msg in grouped_messages
            if not _is_unsupported_media(msg)
        ]
        
        if not valid_messages:
            logging.warning("⚠️ 媒体组中没有可发送的媒体")
            if combined_caption:
                try:
                    return await client.send_message(
                        recipient,
                        combined_caption,
                        reply_to=effective_reply_to,
                    )
                except Exception:
                    pass
            return None

        attempt = 0
        delay = 5
        last_error = None
        
        while attempt < MAX_RETRIES:
            try:
                if any_spoiler:
                    result = await _send_album_with_spoiler(
                        client, recipient, valid_messages,
                        caption=combined_caption or None,
                        reply_to=effective_reply_to,
                    )
                else:
                    files_to_send = [
                        msg for msg in valid_messages
                        if msg.photo or msg.video or msg.gif or msg.document
                    ]
                    if not files_to_send:
                        return await client.send_message(
                            recipient,
                            combined_caption or "空相册",
                            reply_to=effective_reply_to,
                        )
                    result = await client.send_file(
                        recipient, files_to_send,
                        caption=combined_caption or None,
                        reply_to=effective_reply_to,
                        supports_streaming=True,
                        force_document=False,
                        allow_cache=False,
                        parse_mode="md",
                    )
                return result

            except (FileReferenceExpiredError, FileReferenceInvalidError) as e:
                logging.warning(f"⚠️ 文件引用过期，尝试下载重发...")
                # 对媒体组暂不支持下载重发，直接返回失败
                last_error = e
                break
                
            except (MediaInvalidError, MediaEmptyError) as e:
                logging.warning(f"⚠️ 媒体无效: {e}")
                last_error = e
                break
                
            except Exception as e:
                err_str = str(e).upper()
                if "FLOOD_WAIT" in err_str:
                    wait_match = re.search(r'\d+', str(e))
                    wait_sec = int(wait_match.group()) if wait_match else 30
                    await asyncio.sleep(wait_sec + 10)
                else:
                    logging.error(f"❌ 媒体组发送失败 ({attempt+1}/{MAX_RETRIES}): {e}")
                    last_error = e
                attempt += 1
                delay = min(delay * 2, 300)
                await asyncio.sleep(delay)
        
        # 所有重试失败，尝试只发送文字
        if combined_caption:
            try:
                logging.info("📝 媒体发送失败，尝试只发送文字")
                return await client.send_message(
                    recipient,
                    combined_caption,
                    reply_to=effective_reply_to,
                )
            except Exception:
                pass
        
        return None

    # ========== 单条消息发送 ==========
    processed_markup = getattr(tm, 'reply_markup', None)

    # 方式1: 新文件（已处理过的，如加水印）
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
            logging.warning(f"⚠️ 新文件发送失败: {e}")
            try:
                return await client.send_file(
                    recipient, tm.new_file,
                    caption=tm.text,
                    reply_to=effective_reply_to,
                    supports_streaming=True,
                )
            except Exception as e2:
                logging.error(f"❌ 新文件最终失败: {e2}")
                return None

    # 方式2: Spoiler 处理
    if _has_spoiler(tm.message):
        try:
            result = await _send_single_with_spoiler(
                client, recipient, tm.message,
                caption=tm.text, reply_to=effective_reply_to,
            )
            return result
        except (FileReferenceExpiredError, FileReferenceInvalidError):
            logging.warning("⚠️ spoiler 文件引用过期，尝试下载重发")
            result = await _download_and_resend(
                client, recipient, tm.message,
                caption=tm.text, reply_to=effective_reply_to,
            )
            if result:
                return result
        except Exception as e:
            logging.warning(f"⚠️ spoiler 失败: {e}")

    # 方式3: 常规发送（带多重回退）
    last_error = None
    
    for attempt in range(MAX_RETRIES):
        try:
            # 尝试1: 完整发送（带按钮）
            if processed_markup is not None and attempt == 0:
                try:
                    return await client.send_message(
                        recipient,
                        tm.text,
                        file=tm.message.media if tm.message.media else None,
                        buttons=processed_markup,
                        reply_to=effective_reply_to,
                        link_preview=not bool(tm.message.media),
                    )
                except Exception as e:
                    logging.debug(f"带按钮发送失败: {e}")
            
            # 尝试2: 不带按钮发送
            if tm.message.media:
                return await client.send_message(
                    recipient, tm.text,
                    file=tm.message.media,
                    reply_to=effective_reply_to,
                    link_preview=False,
                )
            else:
                return await client.send_message(
                    recipient, tm.text,
                    reply_to=effective_reply_to,
                )
                
        except (FileReferenceExpiredError, FileReferenceInvalidError) as e:
            logging.warning(f"⚠️ 文件引用过期 (attempt {attempt+1}), 尝试下载重发")
            result = await _download_and_resend(
                client, recipient, tm.message,
                caption=tm.text, reply_to=effective_reply_to,
            )
            if result:
                return result
            last_error = e
            
        except (MediaInvalidError, MediaEmptyError) as e:
            logging.warning(f"⚠️ 媒体无效: {e}")
            # 只发送文字
            if tm.text and tm.text.strip():
                try:
                    return await client.send_message(
                        recipient, tm.text,
                        reply_to=effective_reply_to,
                    )
                except Exception:
                    pass
            last_error = e
            break
            
        except Exception as e:
            err_str = str(e).upper()
            if "FLOOD_WAIT" in err_str:
                wait_match = re.search(r'\d+', str(e))
                wait_sec = int(wait_match.group()) if wait_match else 30
                logging.warning(f"⛔ FloodWait {wait_sec}s")
                await asyncio.sleep(wait_sec + 10)
                continue
            
            last_error = e
            logging.error(f"❌ 发送失败 (attempt {attempt+1}/{MAX_RETRIES}): {e}")
            await asyncio.sleep(5 * (attempt + 1))
    
    # 所有方式都失败
    logging.error(f"❌ 消息发送失败: {last_error}")
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
    except Exception:
        return file


def safe_name(string: str) -> str:
    return re.sub(pattern=r"[-!@#$%^&*()\s]", repl="_", string=string)


def match(pattern: str, string: str, regex: bool) -> bool:
    if regex:
        return bool(re.findall(pattern, string))
    return pattern in string


def replace(pattern: str, new: str, string: str, regex: bool) -> str:
    def fmt_repl(matched):
        code = STYLE_CODES.get(new)
        return f"{code}{matched.group(0)}{code}" if code else new

    if regex:
        if new in STYLE_CODES:
            return re.compile(pattern).sub(repl=fmt_repl, string=string)
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
