# nb/utils.py —— 完整修复版

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


def _get_reply_to_top_id(message) -> Optional[int]:
    """获取评论所属的顶层帖子 ID（讨论组中的帖子副本 ID）。"""
    reply_to = getattr(message, 'reply_to', None)
    if reply_to is None:
        return None
    return getattr(reply_to, 'reply_to_top_id', None)


async def get_discussion_message(
    client: TelegramClient,
    channel_id: Union[int, str],
    msg_id: int,
) -> Optional[Message]:
    """获取频道帖子在讨论组中的副本消息。"""
    try:
        result = await client(GetDiscussionMessageRequest(
            peer=channel_id,
            msg_id=msg_id,
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
    """获取频道关联的讨论组 ID。"""
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
#  媒体错误检测
# =====================================================================

def _is_media_invalid_error(e: Exception) -> bool:
    """判断异常是否属于媒体引用失效类错误"""
    error_str = str(e).lower()
    keywords = [
        "file reference",
        "file_reference",
        "media object is invalid",
        "the provided media object is invalid",
        "sendmediarequest",
        "photo_invalid_dimensions",
        "media_invalid",
        "file_reference_expired",
    ]
    return any(kw in error_str for kw in keywords)


# =====================================================================
#  获取用于下载的原始 client
# =====================================================================

def _get_download_client(tm: "NbMessage") -> TelegramClient:
    """获取用于下载媒体的 client。

    如果 sender 插件替换了 tm.client，则 tm.client 和 tm.message.client 不同。
    下载必须用绑定到源消息的 client（即 tm.message.client），
    因为 file_reference 与获取消息的会话绑定。
    """
    msg_client = getattr(tm.message, '_client', None) or getattr(tm.message, 'client', None)
    if msg_client is not None:
        return msg_client
    return tm.client


# =====================================================================
#  重新下载媒体后发送（核心修复）
# =====================================================================

async def _refetch_and_send(
    send_client: TelegramClient,
    download_client: TelegramClient,
    recipient: EntityLike,
    tm: "NbMessage",
    reply_to: Optional[int] = None,
    buttons=None,
) -> Optional[Message]:
    """从源频道重新获取消息、下载媒体、再用发送 client 上传。

    这是处理 file_reference 过期 的终极方案：
    1. 用 download_client 从源频道重新 get_messages（刷新 file_reference）
    2. 下载媒体到内存
    3. 用 send_client 上传到目标
    """
    chat_id = tm.message.chat_id
    msg_id = tm.message.id

    logging.info(f"🔄 重新获取消息 chat={chat_id} msg={msg_id}")

    # ---- 第 1 步：刷新消息对象 ----
    refreshed_msg = None
    try:
        refreshed_msg = await download_client.get_messages(chat_id, ids=msg_id)
    except Exception as e:
        logging.warning(f"⚠️ get_messages 刷新失败: {e}")

    # ---- 第 2 步：下载媒体 ----
    file_bytes = None
    download_source = refreshed_msg if (refreshed_msg and refreshed_msg.media) else tm.message

    # 方法 A: download_media(file=bytes) — 下载到内存
    try:
        file_bytes = await download_source.download_media(file=bytes)
        if file_bytes:
            logging.info(f"✅ 方法A: 下载到内存成功 ({len(file_bytes)} bytes)")
    except Exception as e:
        logging.warning(f"⚠️ 方法A download_media(bytes) 失败: {e}")

    # 方法 B: download_media("") — 下载到临时文件
    if not file_bytes:
        temp_path = None
        try:
            temp_path = await download_source.download_media(file="")
            if temp_path and os.path.exists(temp_path):
                with open(temp_path, "rb") as f:
                    file_bytes = f.read()
                logging.info(f"✅ 方法B: 临时文件下载成功 ({len(file_bytes)} bytes)")
        except Exception as e:
            logging.warning(f"⚠️ 方法B download_media('') 失败: {e}")
        finally:
            if temp_path and os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except Exception:
                    pass

    # 方法 C: 用 download_client.download_media 显式调用
    if not file_bytes:
        try:
            file_bytes = await download_client.download_media(download_source, file=bytes)
            if file_bytes:
                logging.info(f"✅ 方法C: client.download_media 成功 ({len(file_bytes)} bytes)")
        except Exception as e:
            logging.warning(f"⚠️ 方法C client.download_media 失败: {e}")

    # 方法 D: 如果刷新后的消息和原始消息用的是同一个对象都失败了，
    #          尝试用原始 tm.message（如果之前没试过）
    if not file_bytes and download_source is not tm.message:
        try:
            file_bytes = await tm.message.download_media(file=bytes)
            if file_bytes:
                logging.info(f"✅ 方法D: 原始消息下载成功 ({len(file_bytes)} bytes)")
        except Exception as e:
            logging.warning(f"⚠️ 方法D 原始消息下载失败: {e}")

    if not file_bytes:
        logging.error(
            f"❌ 所有下载方式均失败 (chat={chat_id}, msg={msg_id})，"
            f"降级为纯文本发送"
        )
        return await _send_text_only(send_client, recipient, tm, reply_to)

    # ---- 第 3 步：用 send_client 上传 ----
    # 先尝试带按钮
    if buttons is not None:
        try:
            result = await send_client.send_file(
                recipient,
                file_bytes,
                caption=tm.text,
                reply_to=reply_to,
                supports_streaming=True,
                buttons=buttons,
            )
            logging.info("✅ 重新下载后带按钮发送成功")
            return result
        except Exception as e_btn:
            logging.warning(f"⚠️ 带按钮发送失败: {e_btn}")

    # 不带按钮
    try:
        result = await send_client.send_file(
            recipient,
            file_bytes,
            caption=tm.text,
            reply_to=reply_to,
            supports_streaming=True,
        )
        logging.info("✅ 重新下载后发送成功")
        return result
    except Exception as e_final:
        logging.error(f"❌ 重新下载后发送仍然失败: {e_final}")
        return await _send_text_only(send_client, recipient, tm, reply_to)


async def _refetch_album_and_send(
    send_client: TelegramClient,
    download_client: TelegramClient,
    recipient: EntityLike,
    grouped_messages: List[Message],
    caption: Optional[str] = None,
    reply_to: Optional[int] = None,
) -> Optional[List[Message]]:
    """媒体组 file_reference 过期时，重新获取+下载所有文件后发送。"""
    logging.info("🔄 媒体组 file_reference 过期，重新获取并下载...")

    downloaded_files = []

    for msg in grouped_messages:
        if not msg.media:
            continue

        chat_id = msg.chat_id
        msg_id = msg.id
        file_bytes = None

        # 先刷新消息
        refreshed = None
        try:
            refreshed = await download_client.get_messages(chat_id, ids=msg_id)
        except Exception:
            pass

        source = refreshed if (refreshed and refreshed.media) else msg

        # 尝试多种方式下载
        for attempt_label, attempt_func in [
            ("bytes", lambda s: s.download_media(file=bytes)),
            ("file", lambda s: s.download_media(file="")),
            ("client", lambda s: download_client.download_media(s, file=bytes)),
        ]:
            try:
                result = await attempt_func(source)
                if attempt_label == "file":
                    # 从临时文件读取
                    if result and os.path.exists(result):
                        with open(result, "rb") as f:
                            file_bytes = f.read()
                        try:
                            os.remove(result)
                        except Exception:
                            pass
                else:
                    file_bytes = result

                if file_bytes:
                    break
            except Exception:
                continue

        if file_bytes:
            downloaded_files.append(file_bytes)
        else:
            logging.warning(f"⚠️ 媒体组消息 {msg_id} 所有下载方式均失败，跳过")

    if not downloaded_files:
        logging.error("❌ 媒体组中没有任何文件下载成功")
        return None

    try:
        result = await send_client.send_file(
            recipient,
            downloaded_files,
            caption=caption or None,
            reply_to=reply_to,
            supports_streaming=True,
            force_document=False,
            allow_cache=False,
        )
        logging.info(f"✅ 重新下载后媒体组发送成功 ({len(downloaded_files)} 项)")
        return result
    except Exception as e:
        logging.error(f"❌ 重新下载后媒体组发送仍然失败: {e}")
        return None


# =====================================================================
#  降级发送
# =====================================================================

async def _send_text_only(
    client: TelegramClient,
    recipient: EntityLike,
    tm: "NbMessage",
    reply_to: Optional[int] = None,
) -> Optional[Message]:
    """最后的降级方案: 只发文本内容。"""
    text = tm.text
    if not text or not text.strip():
        logging.warning("⚠️ 消息无法发送媒体也无文本内容，跳过")
        return None
    try:
        result = await client.send_message(
            recipient, text, reply_to=reply_to,
        )
        logging.info("✅ 降级为纯文本发送成功")
        return result
    except Exception as e:
        logging.error(f"❌ 纯文本发送也失败: {e}")
        return None


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
    # send_client: 用于发送（可能被 sender 插件替换）
    send_client: TelegramClient = tm.client

    # download_client: 用于下载媒体（始终用原始 client）
    download_client: TelegramClient = _get_download_client(tm)

    effective_reply_to = comment_to_post if comment_to_post else tm.reply_to

    # === 情况 1: 直接转发（保留 forwarded from） ===
    if CONFIG.show_forwarded_from and grouped_messages:
        attempt = 0
        delay = 5
        while attempt < MAX_RETRIES:
            try:
                result = await send_client.forward_messages(recipient, grouped_messages)
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
                        send_client, recipient, grouped_messages,
                        caption=combined_caption or None,
                        reply_to=effective_reply_to,
                    )
                else:
                    files_to_send = [
                        msg for msg in grouped_messages
                        if msg.photo or msg.video or msg.gif or msg.document
                    ]
                    if not files_to_send:
                        return await send_client.send_message(
                            recipient,
                            combined_caption or "空相册",
                            reply_to=effective_reply_to,
                        )
                    result = await send_client.send_file(
                        recipient, files_to_send,
                        caption=combined_caption or None,
                        reply_to=effective_reply_to,
                        supports_streaming=True,
                        force_document=False,
                        allow_cache=False,
                        parse_mode="md",
                    )

                logging.info(
                    f"✅ 媒体组发送成功"
                    f"{'（含 spoiler）' if any_spoiler else ''}"
                    f"{'（评论区）' if comment_to_post else ''}"
                    f" (attempt {attempt+1})"
                )
                return result

            except Exception as e:
                if "FLOOD_WAIT" in str(e).upper():
                    wait_match = re.search(r'\d+', str(e))
                    wait_sec = int(wait_match.group()) if wait_match else 30
                    logging.critical(f"⛔ FloodWait: 等待 {wait_sec} 秒")
                    await asyncio.sleep(wait_sec + 10)
                elif _is_media_invalid_error(e):
                    logging.warning(f"⚠️ 媒体组引用失效 (attempt {attempt+1}): {e}")
                    redownload_result = await _refetch_album_and_send(
                        send_client, download_client,
                        recipient, grouped_messages,
                        caption=combined_caption or None,
                        reply_to=effective_reply_to,
                    )
                    if redownload_result is not None:
                        return redownload_result
                else:
                    logging.error(f"❌ 媒体组发送失败 (attempt {attempt+1}/{MAX_RETRIES}): {e}")
                attempt += 1
                delay = min(delay * 2, 300)
                await asyncio.sleep(delay)
        logging.error(f"❌ 媒体组发送最终失败，已重试 {MAX_RETRIES} 次")
        return None

    # === 情况 3: 单条消息 ===

    processed_markup = getattr(tm, 'reply_markup', None)

    # 3a: 插件生成了新文件
    if tm.new_file:
        try:
            return await send_client.send_file(
                recipient, tm.new_file,
                caption=tm.text,
                reply_to=effective_reply_to,
                supports_streaming=True,
                buttons=processed_markup,
            )
        except Exception as e:
            logging.warning(f"⚠️ 带按钮发送新文件失败: {e}")
            try:
                return await send_client.send_file(
                    recipient, tm.new_file,
                    caption=tm.text,
                    reply_to=effective_reply_to,
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
                send_client, recipient, tm.message,
                caption=tm.text, reply_to=effective_reply_to,
            )
            logging.info("✅ 带 spoiler 单条消息发送成功")
            return result
        except Exception as e:
            if _is_media_invalid_error(e):
                logging.warning(f"⚠️ Spoiler 媒体引用失效: {e}")
                result = await _refetch_and_send(
                    send_client, download_client,
                    recipient, tm,
                    reply_to=effective_reply_to,
                    buttons=processed_markup,
                )
                if result is not None:
                    return result
            logging.warning(f"⚠️ spoiler 发送失败，回退普通模式: {e}")

    # 3c: 普通消息

    async def _try_send_normal() -> Message:
        """尝试直接用 media 引用发送"""
        if processed_markup is not None:
            try:
                return await send_client.send_message(
                    recipient,
                    tm.text,
                    file=tm.message.media if tm.message.media else None,
                    buttons=processed_markup,
                    reply_to=effective_reply_to,
                    link_preview=not bool(tm.message.media),
                )
            except Exception as e:
                logging.warning(f"⚠️ 带按钮发送失败: {e}")
                if tm.message.media:
                    return await send_client.send_message(
                        recipient,
                        tm.text,
                        file=tm.message.media,
                        reply_to=effective_reply_to,
                        link_preview=False,
                    )
                else:
                    return await send_client.send_message(
                        recipient,
                        tm.text,
                        reply_to=effective_reply_to,
                    )
        else:
            if tm.message.media:
                return await send_client.send_message(
                    recipient,
                    tm.text,
                    file=tm.message.media,
                    reply_to=effective_reply_to,
                    link_preview=False,
                )
            else:
                return await send_client.send_message(
                    recipient,
                    tm.text,
                    reply_to=effective_reply_to,
                )

    # ★ 先尝试直接发送，失败后走重新获取+下载+上传流程
    try:
        return await _try_send_normal()
    except Exception as e:
        if _is_media_invalid_error(e) and tm.message.media:
            logging.warning(f"⚠️ 媒体引用失效，重新获取消息: {e}")
            return await _refetch_and_send(
                send_client, download_client,
                recipient, tm,
                reply_to=effective_reply_to,
                buttons=processed_markup,
            )
        else:
            logging.error(f"❌ 消息发送失败: {e}")
            return None


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
    """Delete .session and .session-journal files."""
    for item in os.listdir():
        if item.endswith(".session") or item.endswith(".session-journal"):
            os.remove(item)
            logging.info(f"🧹 删除会话文件: {item}")
