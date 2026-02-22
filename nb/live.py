# nb/live.py

import asyncio
import logging
import random
from typing import Union, List, Optional, Dict

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
    resolve_bot_media_from_message,
    _extract_comment_keyword,
    _auto_comment_keyword,
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


def _dedupe_messages(messages: List[Message]) -> List[Message]:
    seen = set()
    result = []
    for msg in messages:
        if msg.id in seen:
            continue
        seen.add(msg.id)
        result.append(msg)
    return result


def _chunk_list(items: List, size: int) -> List[List]:
    return [items[i:i + size] for i in range(0, len(items), size)]


def _bot_media_allowed(forward) -> bool:
    return forward is None or forward.bot_media_enabled is not False


LIVE_QUEUE: asyncio.Queue = asyncio.Queue()
_queue_task: Optional[asyncio.Task] = None


async def _queue_worker() -> None:
    while True:
        handler, payload = await LIVE_QUEUE.get()
        try:
            await handler(payload)
        except Exception as e:
            logging.error(f"❌ live 队列处理失败: {e}")
        finally:
            LIVE_QUEUE.task_done()
        delay_seconds = random.randint(60, 300)
        logging.info(f"⏸️ live 队列休息 {delay_seconds} 秒")
        await asyncio.sleep(delay_seconds)


async def _enqueue_task(handler, payload) -> None:
    await LIVE_QUEUE.put((handler, payload))



async def _send_bot_media_album(
    dest: int,
    bot_messages: List[Message],
    base_text: Optional[str] = None,
    reply_to: Optional[int] = None,
    comment_to_post: Optional[int] = None,
):
    skip_plugins = ["filter"] if CONFIG.bot_media.ignore_filter else None
    fwded_first = None
    chunks = _chunk_list(bot_messages, 10)
    for idx, chunk_msgs in enumerate(chunks):
        if not chunk_msgs:
            continue
        tms = await apply_plugins_to_group(
            chunk_msgs,
            skip_plugins=skip_plugins,
            fail_open=CONFIG.bot_media.force_forward_on_empty,
            base_text=base_text,
        )
        if not tms:
            continue
        if reply_to is not None and idx == 0:
            tms[0].reply_to = reply_to
        fwded = await send_message(
            dest,
            tms[0],
            grouped_messages=[tm.message for tm in tms],
            grouped_tms=tms,
            comment_to_post=comment_to_post if idx == 0 else None,
        )
        if fwded_first is None:
            fwded_first = fwded
        for tm in tms:
            tm.clear()
    return fwded_first


async def _resolve_comment_dest(
    client: TelegramClient,
    message: Message,
    forward: config.Forward,
) -> Optional[Dict[int, int]]:
    chat_id = message.chat_id
    top_id = _get_reply_to_top_id(message)
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

        dest_post_id = st.get_dest_post_id(src_channel_id, channel_post_id, dest_channel_resolved)
        if dest_post_id is None:
            continue

        if forward.comments.dest_mode == "comments":
            disc_msg = await get_discussion_message(client, dest_channel_resolved, dest_post_id)
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


async def _send_grouped_messages(grouped_id: int) -> None:
    if grouped_id not in st.GROUPED_CACHE:
        return

    chat_messages_map = st.GROUPED_CACHE[grouped_id]
    for chat_id, messages in chat_messages_map.items():
        if chat_id not in config.from_to:
            continue

        dest = config.from_to.get(chat_id)
        forward = config.forward_map.get(chat_id)
        bot_media_allowed = _bot_media_allowed(forward)
        bot_media = []
        if bot_media_allowed:
            trigger_text = None
            for msg in messages:
                bot_media = await resolve_bot_media_from_message(msg.client, msg, forward)
                if bot_media:
                    trigger_text = msg.raw_text or msg.text or ""
                    break
        if bot_media:
            bot_media = _dedupe_messages(bot_media)
            for d in dest:
                try:
                    fwded_msg = await _send_bot_media_album(
                        d,
                        bot_media,
                        base_text=trigger_text,
                    )
                    for original_msg in messages:
                        event_uid = st.EventUid(st.DummyEvent(chat_id, original_msg.id))
                        if event_uid not in st.stored:
                            st.stored[event_uid] = {}
                        st.stored[event_uid][d] = fwded_msg
                except Exception as e:
                    logging.critical(f"🚨 live bot 媒体组播失败: {e}")
            continue
        tms = await apply_plugins_to_group(messages)
        if not tms:
            continue

        tm_template = tms[0]
        for d in dest:
            try:
                fwded_msgs = await send_message(d, tm_template, grouped_messages=[tm.message for tm in tms], grouped_tms=tms)
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


async def _enqueue_grouped_messages(grouped_id: int) -> None:
    await _enqueue_task(_send_grouped_messages, grouped_id)


async def _handle_new_message(event: Union[Message, events.NewMessage]) -> None:
    chat_id = event.chat_id
    if chat_id in config.comment_sources:
        return
    if chat_id not in config.from_to:
        return

    message = event.message
    forward = config.forward_map.get(chat_id)
    bot_media_allowed = _bot_media_allowed(forward)
    auto_comment_allowed = (forward is None or forward.auto_comment_trigger_enabled is not False)
    if bot_media_allowed and auto_comment_allowed:
        keyword = _extract_comment_keyword(message.raw_text or message.text or "", forward)
        if keyword:
            await _auto_comment_keyword(event.client, chat_id, message.id, keyword)
    if message.grouped_id is not None:
        st.add_to_group_cache(chat_id, message.grouped_id, message)
        return

    event_uid = st.EventUid(event)
    if len(st.stored) > const.KEEP_LAST_MANY:
        del st.stored[next(iter(st.stored))]

    dest = config.from_to.get(chat_id)
    bot_media = []
    if bot_media_allowed:
        bot_media = await resolve_bot_media_from_message(event.client, message, forward)
    if bot_media:
        bot_media = _dedupe_messages(bot_media)
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
            try:
                fwded_msg = await _send_bot_media_album(
                    d,
                    bot_media,
                    base_text=message.raw_text or message.text or "",
                    reply_to=reply_to_id,
                )
                if fwded_msg is not None:
                    st.stored[event_uid][d] = fwded_msg
                    fwded_id = _extract_msg_id(fwded_msg)
                    if fwded_id is not None:
                        st.add_post_mapping(chat_id, message.id, d, fwded_id)
            except Exception as e:
                logging.error(f"❌ live bot 媒体发送失败: {e}")
        return
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
        except Exception as e:
            logging.error(f"❌ live 单条发送失败: {e}")

    tm.clear()


async def _handle_comment_message(event: Union[Message, events.NewMessage]) -> None:
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
        channel_post = getattr(message.fwd_from, 'channel_post', None)
        if channel_post:
            st.discussion_to_channel_post[(chat_id, message.id)] = channel_post
            return

    tm = await apply_plugins(message)
    if not tm:
        return

    dest_map = await _resolve_comment_dest(event.client, message, forward)
    if dest_map is None:
        return

    bot_media = []
    bot_media_allowed = _bot_media_allowed(forward)
    if bot_media_allowed:
        bot_media = await resolve_bot_media_from_message(event.client, message, forward)
    if bot_media:
        bot_media = _dedupe_messages(bot_media)

    for dest_discussion_id, dest_top_id in dest_map.items():
        try:
            if bot_media:
                fwded_msg = await _send_bot_media_album(
                    dest_discussion_id,
                    bot_media,
                    base_text=message.raw_text or message.text or "",
                    comment_to_post=dest_top_id,
                )
            else:
                fwded_msg = await send_message(dest_discussion_id, tm, comment_to_post=dest_top_id)
            if fwded_msg is not None:
                st.add_comment_mapping(chat_id, message.id, dest_discussion_id, _extract_msg_id(fwded_msg))
        except Exception as e:
            logging.error(f"❌ 评论转发失败: {e}")

    tm.clear()


async def new_message_handler(event: Union[Message, events.NewMessage]) -> None:
    await _enqueue_task(_handle_new_message, event)


async def comment_message_handler(event: Union[Message, events.NewMessage]) -> None:
    await _enqueue_task(_handle_comment_message, event)


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
                except Exception:
                    pass
        try:
            await event.message.delete()
        except Exception:
            pass
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
    deleted_ids = getattr(event, 'deleted_ids', None)
    if deleted_ids is None:
        deleted_ids = getattr(event, 'deleted_id', None)
        if deleted_ids is not None:
            deleted_ids = [deleted_ids]
        else:
            return

    for deleted_id in deleted_ids:
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
                except Exception:
                    pass
            del st.stored[event_uid]


ALL_EVENTS = {
    "new": (new_message_handler, events.NewMessage()),
    "edited": (edited_message_handler, events.MessageEdited()),
    "deleted": (deleted_message_handler, events.MessageDeleted()),
}


async def _setup_comment_listeners(client: TelegramClient) -> Dict[int, int]:
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
            if dg is None: continue
            if not isinstance(dg, int):
                try: dg = await config.get_id(client, dg)
                except Exception: continue
            comment_sources[dg] = src
            comment_forward_map[dg] = forward
        else:
            dg_id = await get_discussion_group_id(client, src)
            if dg_id is None: continue
            comment_sources[dg_id] = src
            comment_forward_map[dg_id] = forward

    return comment_sources, comment_forward_map


async def start_sync() -> None:
    clean_session_files()
    await load_async_plugins()

    SESSION = get_SESSION()
    client = TelegramClient(SESSION, CONFIG.login.API_ID, CONFIG.login.API_HASH, sequential_updates=CONFIG.live.sequential_updates)

    if CONFIG.login.user_type == 0:
        if not CONFIG.login.BOT_TOKEN:
            logging.error("❌ Bot token 未设置！")
            return
        await client.start(bot_token=CONFIG.login.BOT_TOKEN)
    else:
        await client.start()

    config.is_bot = await client.is_bot()
    logging.info(f"🤖 is_bot = {config.is_bot}")

    ALL_EVENTS.update(get_events())
    await config.load_admins(client)
    config.from_to = await config.load_from_to(client, CONFIG.forwards)
    config.forward_map = await config.load_forward_map(client, CONFIG.forwards)

    has_comments = any(f.use_this and f.comments.enabled for f in CONFIG.forwards)
    if has_comments:
        comment_src, comment_fwd = await _setup_comment_listeners(client)
        config.comment_sources = comment_src
        config.comment_forward_map = comment_fwd
        if comment_src:
            client.add_event_handler(comment_message_handler, events.NewMessage(chats=list(comment_src.keys())))

    for key, val in ALL_EVENTS.items():
        if not CONFIG.live.delete_sync and key == "deleted":
            continue
        client.add_event_handler(*val)

    global _queue_task
    if _queue_task is None or _queue_task.done():
        _queue_task = asyncio.create_task(_queue_worker())

    logging.info("🟢 live 模式启动完成")
    await client.run_until_disconnected()
