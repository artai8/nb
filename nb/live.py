import asyncio
import logging
from typing import Dict, List, Optional, Union

from telethon import TelegramClient, events
from telethon.tl.custom.message import Message

from nb import config, const
from nb import storage as st
from nb.bot import get_events
from nb.config import CONFIG, get_SESSION
from nb.plugins import apply_plugins, apply_plugins_to_group, load_async_plugins
from nb.utils import (
    clean_session_files, extract_msg_id, send_message,
    _get_reply_to_msg_id, _get_reply_to_top_id,
    get_discussion_message, get_discussion_group_id,
)

COMMENT_GROUPED_CACHE: Dict[int, Dict[int, List[Message]]] = {}
COMMENT_GROUPED_TIMERS: Dict[int, asyncio.TimerHandle] = {}
COMMENT_GROUPED_TIMEOUT = 2.0


async def _flush_comment_group(grouped_id):
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
            tms = await apply_plugins_to_group(messages)
            if not tms or tms[0] is None:
                continue
            dest_map = await _resolve_comment_dest(messages[0].client, messages[0], forward)
            if dest_map is None:
                continue
            for dest_id, dest_top_id in dest_map.items():
                try:
                    fwded = await send_message(dest_id, tms[0], grouped_messages=[tm.message for tm in tms], grouped_tms=tms, comment_to_post=dest_top_id)
                    if fwded is not None:
                        st.add_comment_mapping(chat_id, messages[0].id, dest_id, extract_msg_id(fwded))
                except Exception as e:
                    logging.error(f"评论媒体组转发失败: {e}")
            for tm in tms:
                tm.clear()
    except Exception as e:
        logging.exception(f"处理评论媒体组 {grouped_id} 失败: {e}")
    finally:
        COMMENT_GROUPED_CACHE.pop(grouped_id, None)
        COMMENT_GROUPED_TIMERS.pop(grouped_id, None)


def _add_comment_to_group_cache(chat_id, grouped_id, message):
    if grouped_id not in COMMENT_GROUPED_CACHE:
        COMMENT_GROUPED_CACHE[grouped_id] = {}
    if chat_id not in COMMENT_GROUPED_CACHE[grouped_id]:
        COMMENT_GROUPED_CACHE[grouped_id][chat_id] = []
    COMMENT_GROUPED_CACHE[grouped_id][chat_id].append(message)
    if grouped_id in COMMENT_GROUPED_TIMERS:
        COMMENT_GROUPED_TIMERS[grouped_id].cancel()
    loop = asyncio.get_running_loop()
    COMMENT_GROUPED_TIMERS[grouped_id] = loop.call_later(
        COMMENT_GROUPED_TIMEOUT,
        lambda gid=grouped_id: asyncio.ensure_future(_flush_comment_group(gid)),
    )


async def _try_resolve_top_id(client, chat_id, msg_id):
    try:
        msg = await client.get_messages(chat_id, ids=msg_id)
        if msg and hasattr(msg, 'fwd_from') and msg.fwd_from:
            cp = getattr(msg.fwd_from, 'channel_post', None)
            if cp:
                st.discussion_to_channel_post[(chat_id, msg_id)] = cp
                return msg_id
    except Exception:
        pass
    return None


async def _resolve_comment_dest(client, message, forward) -> Optional[Dict[int, int]]:
    chat_id = message.chat_id
    top_id = _get_reply_to_top_id(message)
    if top_id is None:
        reply_msg_id = _get_reply_to_msg_id(message)
        if reply_msg_id is not None:
            if (chat_id, reply_msg_id) in st.discussion_to_channel_post:
                top_id = reply_msg_id
            else:
                resolved = await _try_resolve_top_id(client, chat_id, reply_msg_id)
                if resolved:
                    top_id = resolved
    if top_id is None:
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
        except Exception:
            pass
    if channel_post_id is None:
        return None

    result = {}
    for dest_channel_id in forward.dest:
        dest_resolved = dest_channel_id
        if not isinstance(dest_resolved, int):
            try:
                dest_resolved = await config.get_id(client, dest_channel_id)
            except Exception:
                continue
        dest_post_id = st.get_dest_post_id(src_channel_id, channel_post_id, dest_resolved)
        if dest_post_id is None:
            continue
        if forward.comments.dest_mode == "comments":
            disc_msg = await get_discussion_message(client, dest_resolved, dest_post_id)
            if disc_msg:
                result[disc_msg.chat_id] = disc_msg.id
            else:
                result[dest_resolved] = dest_post_id
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


async def _send_grouped_messages(grouped_id):
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
        for d in dest:
            try:
                fwded_msgs = await send_message(d, tms[0], grouped_messages=[tm.message for tm in tms], grouped_tms=tms)
                for i, msg in enumerate(messages):
                    uid = st.EventUid(st.DummyEvent(chat_id, msg.id))
                    if uid not in st.stored:
                        st.stored[uid] = {}
                    if isinstance(fwded_msgs, list) and i < len(fwded_msgs):
                        st.stored[uid][d] = fwded_msgs[i]
                    elif not isinstance(fwded_msgs, list):
                        st.stored[uid][d] = fwded_msgs
                    if i == 0:
                        fid = extract_msg_id(fwded_msgs[0] if isinstance(fwded_msgs, list) else fwded_msgs)
                        if fid is not None:
                            st.add_post_mapping(chat_id, msg.id, d, fid)
            except Exception as e:
                logging.critical(f"live 组播失败: {e}")
    st.GROUPED_CACHE.pop(grouped_id, None)
    st.GROUPED_TIMERS.pop(grouped_id, None)
    st.GROUPED_MAPPING.pop(grouped_id, None)


async def new_message_handler(event):
    chat_id = event.chat_id
    if chat_id not in config.from_to:
        return
    message = event.message
    if message.grouped_id is not None:
        st.add_to_group_cache(chat_id, message.grouped_id, message)
        return
    uid = st.EventUid(event)
    if len(st.stored) > const.KEEP_LAST_MANY:
        del st.stored[next(iter(st.stored))]
    dest = config.from_to.get(chat_id)
    tm = await apply_plugins(message)
    if not tm:
        return
    st.stored[uid] = {}
    for d in dest:
        reply_to_id = None
        if event.is_reply:
            rmid = _get_reply_to_msg_id(event.message)
            if rmid is not None:
                r_uid = st.EventUid(st.DummyEvent(chat_id, rmid))
                if r_uid in st.stored:
                    reply_to_id = extract_msg_id(st.stored[r_uid].get(d))
        tm.reply_to = reply_to_id
        try:
            fwded = await send_message(d, tm)
            if fwded is not None:
                st.stored[uid][d] = fwded
                fid = extract_msg_id(fwded)
                if fid is not None:
                    st.add_post_mapping(chat_id, message.id, d, fid)
        except Exception as e:
            logging.error(f"live 发送失败: {e}")
    tm.clear()


async def comment_message_handler(event):
    chat_id = event.chat_id
    message = event.message
    if chat_id not in config.comment_sources:
        return
    forward = config.comment_forward_map.get(chat_id)
    if forward is None or not forward.comments.enabled:
        return
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
    if hasattr(message, 'fwd_from') and message.fwd_from:
        cp = getattr(message.fwd_from, 'channel_post', None)
        if cp:
            st.discussion_to_channel_post[(chat_id, message.id)] = cp
            return
    if message.grouped_id is not None:
        _add_comment_to_group_cache(chat_id, message.grouped_id, message)
        return
    tm = await apply_plugins(message)
    if not tm:
        return
    dest_map = await _resolve_comment_dest(event.client, message, forward)
    if dest_map is None:
        return
    for dest_id, dest_top_id in dest_map.items():
        try:
            fwded = await send_message(dest_id, tm, comment_to_post=dest_top_id)
            if fwded is not None:
                st.add_comment_mapping(chat_id, message.id, dest_id, extract_msg_id(fwded))
        except Exception as e:
            logging.error(f"评论转发失败: {e}")
    tm.clear()


async def edited_message_handler(event):
    chat_id = event.chat_id
    if chat_id not in config.from_to:
        return
    uid = st.EventUid(event)
    if uid not in st.stored:
        return
    if CONFIG.live.delete_on_edit and event.message.text == CONFIG.live.delete_on_edit:
        for d in config.from_to.get(chat_id, []):
            mid = extract_msg_id(st.stored[uid].get(d))
            if mid is not None:
                try:
                    await event.client.delete_messages(d, mid)
                except Exception:
                    pass
        try:
            await event.message.delete()
        except Exception:
            pass
        del st.stored[uid]
        return
    tm = await apply_plugins(event.message)
    if not tm:
        return
    for d in config.from_to.get(chat_id, []):
        mid = extract_msg_id(st.stored[uid].get(d))
        if mid is not None:
            try:
                await event.client.edit_message(d, mid, tm.text)
            except Exception:
                pass
    tm.clear()


async def deleted_message_handler(event):
    for did in event.deleted_ids:
        for chat_id in list(config.from_to.keys()):
            uid = st.EventUid(st.DummyEvent(chat_id, did))
            if uid not in st.stored:
                continue
            for d, fwded in st.stored[uid].items():
                mid = extract_msg_id(fwded)
                if mid is not None:
                    try:
                        await event.client.delete_messages(d, mid)
                    except Exception:
                        pass
            del st.stored[uid]


ALL_EVENTS = {
    "new": (new_message_handler, events.NewMessage()),
    "edited": (edited_message_handler, events.MessageEdited()),
    "deleted": (deleted_message_handler, events.MessageDeleted()),
}


async def _setup_comment_listeners(client):
    comment_sources = {}
    comment_forward_map = {}
    for forward in CONFIG.forwards:
        if not forward.use_this or not forward.comments.enabled:
            continue
        src = forward.source
        if not isinstance(src, int):
            try:
                src = await config.get_id(client, forward.source)
            except Exception:
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
        else:
            dg_id = await get_discussion_group_id(client, src)
            if dg_id is None:
                continue
            comment_sources[dg_id] = src
            comment_forward_map[dg_id] = forward
    return comment_sources, comment_forward_map


async def _preload_recent_post_mappings(client):
    for discussion_id, src_channel_id in config.comment_sources.items():
        try:
            count = 0
            async for msg in client.iter_messages(discussion_id, limit=500):
                if hasattr(msg, 'fwd_from') and msg.fwd_from:
                    cp = getattr(msg.fwd_from, 'channel_post', None)
                    if cp:
                        st.discussion_to_channel_post[(discussion_id, msg.id)] = cp
                        count += 1
            logging.info(f"预加载 {count} 个帖子映射: 讨论组 {discussion_id}")
        except Exception as e:
            logging.warning(f"预加载帖子映射失败 (讨论组={discussion_id}): {e}")


async def start_sync():
    clean_session_files()
    await load_async_plugins()
    SESSION = get_SESSION()
    client = TelegramClient(SESSION, CONFIG.login.API_ID, CONFIG.login.API_HASH, sequential_updates=CONFIG.live.sequential_updates)
    if CONFIG.login.user_type == 0:
        if not CONFIG.login.BOT_TOKEN:
            return
        await client.start(bot_token=CONFIG.login.BOT_TOKEN)
    else:
        await client.start()
    config.is_bot = await client.is_bot()
    ALL_EVENTS.update(get_events())
    await config.load_admins(client)
    config.from_to = await config.load_from_to(client, CONFIG.forwards)
    if not config.from_to:
        return
    has_comments = any(f.use_this and f.comments.enabled for f in CONFIG.forwards)
    if has_comments:
        cs, cf = await _setup_comment_listeners(client)
        config.comment_sources = cs
        config.comment_forward_map = cf
        if cs:
            await _preload_recent_post_mappings(client)
            client.add_event_handler(comment_message_handler, events.NewMessage(chats=list(cs.keys())))
    for key, val in ALL_EVENTS.items():
        if not CONFIG.live.delete_sync and key == "deleted":
            continue
        client.add_event_handler(*val)
    logging.info("live 模式启动完成")
    await client.run_until_disconnected()
