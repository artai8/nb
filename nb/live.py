# nb/live.py

import asyncio
import logging
from typing import Union, List, Optional, Dict
from collections import defaultdict

from telethon import TelegramClient, events
from telethon.tl.custom.message import Message

from nb import config, const
from nb import storage as st
from nb.bot import get_events
from nb.config import CONFIG, get_SESSION
from nb.plugins import apply_plugins, apply_plugins_to_group, load_async_plugins
from nb.utils import (
    clean_session_files,
    send_message,
    _get_reply_to_msg_id,
    _get_reply_to_top_id,
    get_discussion_message,
    get_discussion_group_id,
)


def _extract_msg_id(fwded) -> Optional[int]:
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


# =====================================================================
#  评论区媒体组缓存（独立于主消息的媒体组缓存）
# =====================================================================

COMMENT_GROUPED_CACHE: Dict[int, Dict[int, List[Message]]] = {}
COMMENT_GROUPED_TIMERS: Dict[int, asyncio.TimerHandle] = {}
COMMENT_GROUPED_TIMEOUT = 2.0


async def _flush_comment_group(grouped_id: int) -> None:
    """发送缓存的评论媒体组"""
    if grouped_id not in COMMENT_GROUPED_CACHE:
        return

    try:
        chat_messages_map = COMMENT_GROUPED_CACHE[grouped_id]

        for chat_id, messages in chat_messages_map.items():
            if chat_id not in config.comment_sources:
                continue

            forward = config.comment_forward_map.get(chat_id)
            if forward is None or not forward.comments.enabled:
                continue

            # 应用插件到整个媒体组
            tms = await apply_plugins_to_group(messages)
            if not tms:
                continue

            tm_template = tms[0]
            if tm_template is None:
                continue

            # 解析目标
            dest_map = await _resolve_comment_dest(
                messages[0].client, messages[0], forward
            )
            if dest_map is None:
                logging.debug(f"💬 评论媒体组 {grouped_id} 无法找到目标帖子")
                continue

            # 发送到每个目标（作为媒体组）
            for dest_discussion_id, dest_top_id in dest_map.items():
                try:
                    fwded_msg = await send_message(
                        dest_discussion_id,
                        tm_template,
                        grouped_messages=[tm.message for tm in tms],
                        grouped_tms=tms,
                        comment_to_post=dest_top_id,
                    )
                    if fwded_msg is not None:
                        st.add_comment_mapping(
                            chat_id, messages[0].id,
                            dest_discussion_id, _extract_msg_id(fwded_msg)
                        )
                        logging.info(
                            f"✅ 评论媒体组转发成功: {chat_id}/group={grouped_id} → "
                            f"{dest_discussion_id} ({len(messages)} 条)"
                        )
                except Exception as e:
                    logging.error(f"❌ 评论媒体组转发失败: {e}")

            for tm in tms:
                tm.clear()

    except Exception as e:
        logging.exception(f"❌ 处理评论媒体组 {grouped_id} 失败: {e}")
    finally:
        COMMENT_GROUPED_CACHE.pop(grouped_id, None)
        COMMENT_GROUPED_TIMERS.pop(grouped_id, None)


def _add_comment_to_group_cache(
    chat_id: int, grouped_id: int, message: Message
) -> None:
    """将评论消息加入媒体组缓存"""
    if grouped_id not in COMMENT_GROUPED_CACHE:
        COMMENT_GROUPED_CACHE[grouped_id] = {}
    if chat_id not in COMMENT_GROUPED_CACHE[grouped_id]:
        COMMENT_GROUPED_CACHE[grouped_id][chat_id] = []
    COMMENT_GROUPED_CACHE[grouped_id][chat_id].append(message)

    # 重置定时器
    if grouped_id in COMMENT_GROUPED_TIMERS:
        COMMENT_GROUPED_TIMERS[grouped_id].cancel()

    loop = asyncio.get_running_loop()
    COMMENT_GROUPED_TIMERS[grouped_id] = loop.call_later(
        COMMENT_GROUPED_TIMEOUT,
        lambda gid=grouped_id: asyncio.ensure_future(_flush_comment_group(gid)),
    )


# =====================================================================
#  评论区：解析讨论组消息并找到对应的目标帖子
# =====================================================================


async def _resolve_comment_dest(
    client: TelegramClient,
    message: Message,
    forward: config.Forward,
) -> Optional[Dict[int, int]]:
    chat_id = message.chat_id

    top_id = _get_reply_to_top_id(message)
    if top_id is None:
        logging.debug(f"消息 {message.id} 没有 reply_to_top_id")
        return None

    src_channel_id = config.comment_sources.get(chat_id)
    if src_channel_id is None:
        return None

    channel_post_id = st.discussion_to_channel_post.get((chat_id, top_id))

    if channel_post_id is None:
        try:
            top_msg = await client.get_messages(chat_id, ids=top_id)
            if top_msg and hasattr(top_msg, 'fwd_from') and top_msg.fwd_from:
                channel_post_id = getattr(top_msg.fwd_from, 'channel_post', None)
                if channel_post_id:
                    st.discussion_to_channel_post[(chat_id, top_id)] = channel_post_id
        except Exception as e:
            logging.warning(f"⚠️ 反查帖子失败: {e}")

    if channel_post_id is None:
        return None

    result = {}
    for dest_channel_id in forward.dest:
        dest_channel_resolved = dest_channel_id
        if not isinstance(dest_channel_resolved, int):
            try:
                dest_channel_resolved = await config.get_id(client, dest_channel_id)
            except Exception:
                continue

        dest_post_id = st.get_dest_post_id(
            src_channel_id, channel_post_id, dest_channel_resolved
        )
        if dest_post_id is None:
            continue

        if forward.comments.dest_mode == "comments":
            disc_msg = await get_discussion_message(
                client, dest_channel_resolved, dest_post_id
            )
            if disc_msg:
                result[disc_msg.chat_id] = disc_msg.id
        elif forward.comments.dest_mode == "discussion":
            for dg in forward.comments.dest_discussion_groups:
                dg_id = dg
                if not isinstance(dg_id, int):
                    try:
                        dg_id = await config.get_id(client, dg)
                    except Exception:
                        continue
                result[dg_id] = None

    return result if result else None


# =====================================================================
#  媒体组发送（主消息）
# =====================================================================


async def _send_grouped_messages(grouped_id: int) -> None:
    if grouped_id not in st.GROUPED_CACHE:
        return

    chat_messages_map = st.GROUPED_CACHE[grouped_id]
    for chat_id, messages in chat_messages_map.items():
        if chat_id not in config.from_to:
            continue

        dest = config.from_to.get(chat_id)

        tms = await apply_plugins_to_group(messages)
        if not tms:
            continue

        tm_template = tms[0]

        for d in dest:
            try:
                fwded_msgs = await send_message(
                    d,
                    tm_template,
                    grouped_messages=[tm.message for tm in tms],
                    grouped_tms=tms,
                )

                for i, original_msg in enumerate(messages):
                    event_uid = st.EventUid(st.DummyEvent(chat_id, original_msg.id))
                    if event_uid not in st.stored:
                        st.stored[event_uid] = {}
                    if isinstance(fwded_msgs, list) and i < len(fwded_msgs):
                        st.stored[event_uid][d] = fwded_msgs[i]
                    elif not isinstance(fwded_msgs, list):
                        st.stored[event_uid][d] = fwded_msgs

            except Exception as e:
                logging.critical(f"🚨 live 模式组播失败: {e}")

    st.GROUPED_CACHE.pop(grouped_id, None)
    st.GROUPED_TIMERS.pop(grouped_id, None)
    st.GROUPED_MAPPING.pop(grouped_id, None)


# =====================================================================
#  主消息处理
# =====================================================================


async def new_message_handler(event: Union[Message, events.NewMessage]) -> None:
    chat_id = event.chat_id
    if chat_id not in config.from_to:
        return

    message = event.message
    if message.grouped_id is not None:
        st.add_to_group_cache(chat_id, message.grouped_id, message)
        return

    event_uid = st.EventUid(event)
    if len(st.stored) > const.KEEP_LAST_MANY:
        del st.stored[next(iter(st.stored))]

    dest = config.from_to.get(chat_id)
    tm = await apply_plugins(message)
    if not tm:
        return

    st.stored[event_uid] = {}
    for d in dest:
        reply_to_id = None
        if event.is_reply:
            reply_msg_id = _get_reply_to_msg_id(event.message)
            if reply_msg_id is not None:
                r_event = st.DummyEvent(chat_id, reply_msg_id)
                r_event_uid = st.EventUid(r_event)
                if r_event_uid in st.stored:
                    fwded_reply = st.stored[r_event_uid].get(d)
                    reply_to_id = _extract_msg_id(fwded_reply)
        tm.reply_to = reply_to_id

        try:
            fwded_msg = await send_message(d, tm)
            if fwded_msg is not None:
                st.stored[event_uid][d] = fwded_msg
                fwded_id = _extract_msg_id(fwded_msg)
                if fwded_id is not None:
                    st.add_post_mapping(chat_id, message.id, d, fwded_id)
            else:
                logging.warning(f"⚠️ 发送返回 None, dest={d}, msg={message.id}")
        except Exception as e:
            logging.error(f"❌ live 单条发送失败: {e}")

    tm.clear()


# =====================================================================
#  评论区消息处理器（★ 支持媒体组）
# =====================================================================


async def comment_message_handler(event: Union[Message, events.NewMessage]) -> None:
    """处理讨论组（评论区）中的新消息。支持媒体组。"""
    chat_id = event.chat_id
    message = event.message

    if chat_id not in config.comment_sources:
        return

    forward = config.comment_forward_map.get(chat_id)
    if forward is None or not forward.comments.enabled:
        return

    # 过滤
    if forward.comments.only_media and not message.media:
        return

    if not forward.comments.include_text_comments and not message.media:
        return

    if forward.comments.skip_bot_comments:
        try:
            sender = await event.get_sender()
            if sender and getattr(sender, 'bot', False):
                return
        except Exception:
            pass

    # 检查是否是频道帖子副本（不是用户评论）
    if hasattr(message, 'fwd_from') and message.fwd_from:
        channel_post = getattr(message.fwd_from, 'channel_post', None)
        if channel_post:
            st.discussion_to_channel_post[(chat_id, message.id)] = channel_post
            logging.info(
                f"📎 记录帖子副本: discussion({chat_id}, {message.id}) "
                f"→ channel_post {channel_post}"
            )
            return

    # ★★★ 媒体组：加入缓存，等待超时后整组发送 ★★★
    if message.grouped_id is not None:
        logging.info(
            f"💬 评论媒体组消息: chat={chat_id}, "
            f"msg={message.id}, grouped_id={message.grouped_id}"
        )
        _add_comment_to_group_cache(chat_id, message.grouped_id, message)
        return

    # ★★★ 单条消息：直接发送 ★★★
    tm = await apply_plugins(message)
    if not tm:
        return

    dest_map = await _resolve_comment_dest(event.client, message, forward)
    if dest_map is None:
        logging.debug(f"💬 评论 {message.id} 无法找到目标帖子")
        return

    for dest_discussion_id, dest_top_id in dest_map.items():
        try:
            fwded_msg = await send_message(
                dest_discussion_id,
                tm,
                comment_to_post=dest_top_id,
            )
            if fwded_msg is not None:
                st.add_comment_mapping(
                    chat_id, message.id,
                    dest_discussion_id, _extract_msg_id(fwded_msg)
                )
                logging.info(
                    f"💬 评论转发成功: {chat_id}/{message.id} → "
                    f"{dest_discussion_id}"
                )
        except Exception as e:
            logging.error(f"❌ 评论转发失败: {e}")

    tm.clear()


# =====================================================================
#  编辑和删除处理器
# =====================================================================


async def edited_message_handler(event) -> None:
    chat_id = event.chat_id
    if chat_id not in config.from_to:
        return

    event_uid = st.EventUid(event)
    if event_uid not in st.stored:
        return

    if CONFIG.live.delete_on_edit and event.message.text == CONFIG.live.delete_on_edit:
        dest = config.from_to.get(chat_id, [])
        for d in dest:
            fwded = st.stored[event_uid].get(d)
            mid = _extract_msg_id(fwded)
            if mid is not None:
                try:
                    await event.client.delete_messages(d, mid)
                except Exception as e:
                    logging.error(f"❌ delete_on_edit 删除失败: {e}")
        try:
            await event.message.delete()
        except Exception as e:
            logging.error(f"❌ delete_on_edit 删除源失败: {e}")
        del st.stored[event_uid]
        return

    dest = config.from_to.get(chat_id, [])
    tm = await apply_plugins(event.message)
    if not tm:
        return

    for d in dest:
        fwded = st.stored[event_uid].get(d)
        mid = _extract_msg_id(fwded)
        if mid is not None:
            try:
                await event.client.edit_message(d, mid, tm.text)
            except Exception as e:
                logging.error(f"❌ 编辑同步失败: {e}")
    tm.clear()


async def deleted_message_handler(event) -> None:
    for deleted_id in event.deleted_ids:
        for chat_id in list(config.from_to.keys()):
            r_event = st.DummyEvent(chat_id, deleted_id)
            event_uid = st.EventUid(r_event)
            if event_uid not in st.stored:
                continue
            dest_map = st.stored[event_uid]
            for d, fwded in dest_map.items():
                mid = _extract_msg_id(fwded)
                if mid is None:
                    continue
                try:
                    await event.client.delete_messages(d, mid)
                except Exception as e:
                    logging.error(f"❌ 删除同步失败: {e}")
            del st.stored[event_uid]


# =====================================================================
#  事件注册
# =====================================================================


ALL_EVENTS = {
    "new": (new_message_handler, events.NewMessage()),
    "edited": (edited_message_handler, events.MessageEdited()),
    "deleted": (deleted_message_handler, events.MessageDeleted()),
}


async def _setup_comment_listeners(client: TelegramClient):
    comment_sources = {}
    comment_forward_map = {}

    for forward in CONFIG.forwards:
        if not forward.use_this or not forward.comments.enabled:
            continue

        src = forward.source
        if not isinstance(src, int):
            try:
                src = await config.get_id(client, forward.source)
            except Exception as e:
                logging.error(f"❌ 无法解析源 {forward.source}: {e}")
                continue

        if forward.comments.source_mode == "discussion":
            dg = forward.comments.source_discussion_group
            if dg is None:
                continue
            if not isinstance(dg, int):
                try:
                    dg = await config.get_id(client, dg)
                except Exception:
                    continue
            comment_sources[dg] = src
            comment_forward_map[dg] = forward
            logging.info(f"💬 监听讨论组 {dg} (手动, 源频道 {src})")

        else:
            dg_id = await get_discussion_group_id(client, src)
            if dg_id is None:
                logging.warning(f"⚠️ 频道 {src} 没有讨论组")
                continue
            comment_sources[dg_id] = src
            comment_forward_map[dg_id] = forward
            logging.info(f"💬 监听讨论组 {dg_id} (自动, 源频道 {src})")

    return comment_sources, comment_forward_map


async def start_sync() -> None:
    clean_session_files()
    await load_async_plugins()

    SESSION = get_SESSION()
    client = TelegramClient(
        SESSION,
        CONFIG.login.API_ID,
        CONFIG.login.API_HASH,
        sequential_updates=CONFIG.live.sequential_updates,
    )

    if CONFIG.login.user_type == 0:
        if not CONFIG.login.BOT_TOKEN:
            logging.error("❌ Bot token 未设置")
            return
        await client.start(bot_token=CONFIG.login.BOT_TOKEN)
    else:
        await client.start()

    config.is_bot = await client.is_bot()
    logging.info(f"🤖 is_bot = {config.is_bot}")

    ALL_EVENTS.update(get_events())
    await config.load_admins(client)
    config.from_to = await config.load_from_to(client, CONFIG.forwards)

    if not config.from_to:
        logging.error("❌ 没有有效的转发连接")
        return

    # 评论区监听
    has_comments = any(
        f.use_this and f.comments.enabled for f in CONFIG.forwards
    )
    if has_comments:
        comment_src, comment_fwd = await _setup_comment_listeners(client)
        config.comment_sources = comment_src
        config.comment_forward_map = comment_fwd

        if comment_src:
            discussion_group_ids = list(comment_src.keys())
            logging.info(f"💬 评论区监听: {discussion_group_ids}")

            client.add_event_handler(
                comment_message_handler,
                events.NewMessage(chats=discussion_group_ids),
            )
            logging.info("✅ 注册评论区事件处理器（支持媒体组）")
        else:
            logging.warning("⚠️ 启用了评论区功能但没有找到讨论组")

    for key, val in ALL_EVENTS.items():
        if not CONFIG.live.delete_sync and key == "deleted":
            continue
        client.add_event_handler(*val)
        logging.info(f"✅ 注册事件处理器: {key}")

    logging.info("🟢 live 模式启动完成")
    await client.run_until_disconnected()
