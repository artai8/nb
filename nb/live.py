import asyncio
import logging
from typing import Dict, List, Optional
from telethon import TelegramClient, events
from telethon.tl.custom.message import Message
from nb import config, const
from nb import storage as st
from nb.bot import get_events
from nb.config import CONFIG, get_SESSION
from nb.plugins import apply_plugins, apply_plugins_to_group, load_async_plugins
from nb.utils import (
    clean_session_files, extract_msg_id, send_message,
    _get_reply_to_msg_id, _get_reply_to_top_id, _extract_channel_post,
    get_discussion_message, get_discussion_group_id,
    preload_discussion_mappings,
)

COMMENT_GROUPED_CACHE: Dict[int, Dict[int, List[Message]]] = {}
COMMENT_GROUPED_TIMERS: Dict[int, asyncio.TimerHandle] = {}
COMMENT_GROUPED_TIMEOUT = 3.0


def _align_messages_and_tms(messages, tms):
    """确保 messages 和 tms 数量一致，返回对齐后的 messages 列表。"""
    if len(tms) == len(messages):
        return messages
    logging.debug(f"[align] 插件过滤: {len(messages)} -> {len(tms)}")
    return [tm.message for tm in tms]


async def _send_grouped_messages(grouped_id):
    """发送缓冲的媒体组消息"""
    if grouped_id not in st.GROUPED_CACHE:
        return

    chat_messages_map = st.GROUPED_CACHE[grouped_id]

    for chat_id, messages in chat_messages_map.items():
        if chat_id not in config.from_to:
            continue

        dest = config.from_to.get(chat_id)

        tms = await apply_plugins_to_group(messages)
        if not tms:
            logging.warning(f"[grouped] {grouped_id} 所有消息被过滤")
            continue

        aligned_messages = _align_messages_and_tms(messages, tms)

        logging.debug(
            f"[grouped] id={grouped_id} aligned={len(aligned_messages)} tms={len(tms)} "
            f"types=[{', '.join(tm.file_type for tm in tms)}] "
            f"texts=[{', '.join(repr(tm.text[:30]) if tm.text else 'None' for tm in tms)}]"
        )

        for d in dest:
            try:
                fwded_msgs = await send_message(
                    d, tms[0],
                    grouped_messages=aligned_messages,
                    grouped_tms=tms,
                )

                if fwded_msgs:
                    fwded_list = fwded_msgs if isinstance(fwded_msgs, list) else [fwded_msgs]

                    for i, msg in enumerate(aligned_messages):
                        uid = st.EventUid(st.DummyEvent(chat_id, msg.id))
                        if uid not in st.stored:
                            st.stored[uid] = {}
                        if i < len(fwded_list):
                            st.stored[uid][d] = fwded_list[i]
                        else:
                            st.stored[uid][d] = fwded_list[-1] if fwded_list else None

                    if fwded_list:
                        fid = extract_msg_id(fwded_list[0])
                        if fid:
                            for msg in aligned_messages:
                                st.add_post_mapping(chat_id, msg.id, d, fid)

            except Exception as e:
                logging.error(f"[grouped] 发送失败: {e}", exc_info=True)

        for tm in tms:
            tm.clear()

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
            if rmid:
                r_uid = st.EventUid(st.DummyEvent(chat_id, rmid))
                if r_uid in st.stored:
                    reply_to_id = extract_msg_id(st.stored[r_uid].get(d))
        tm.reply_to = reply_to_id
        try:
            fwded = await send_message(d, tm)
            if fwded:
                st.stored[uid][d] = fwded
                fid = extract_msg_id(fwded)
                if fid:
                    st.add_post_mapping(chat_id, message.id, d, fid)
        except Exception as e:
            logging.error(f"发送失败: {e}")
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
            if mid:
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
        if mid:
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
                if mid:
                    try:
                        await event.client.delete_messages(d, mid)
                    except Exception:
                        pass
            del st.stored[uid]


async def _resolve_channel_post_id(client, chat_id, message) -> Optional[int]:
    top_id = _get_reply_to_top_id(message)
    if top_id:
        cp = st.get_channel_post_id(chat_id, top_id)
        if cp:
            return cp
        try:
            top_msg = await client.get_messages(chat_id, ids=top_id)
            if top_msg:
                fwd = _extract_channel_post(top_msg)
                if fwd:
                    st.add_discussion_mapping(chat_id, top_id, fwd)
                    return fwd
        except Exception as e:
            logging.debug(f"获取 top_msg 失败: {e}")

    reply_id = _get_reply_to_msg_id(message)
    if reply_id:
        cp = st.get_channel_post_id(chat_id, reply_id)
        if cp:
            return cp
        try:
            reply_msg = await client.get_messages(chat_id, ids=reply_id)
            if reply_msg:
                fwd = _extract_channel_post(reply_msg)
                if fwd:
                    st.add_discussion_mapping(chat_id, reply_id, fwd)
                    return fwd
        except Exception as e:
            logging.debug(f"获取 reply_msg 失败: {e}")

    if reply_id or top_id:
        search_id = top_id or reply_id
        try:
            async for msg in client.iter_messages(chat_id, min_id=max(1, search_id - 5), max_id=search_id + 1):
                fwd = _extract_channel_post(msg)
                if fwd:
                    st.add_discussion_mapping(chat_id, msg.id, fwd)
                    return fwd
        except Exception as e:
            logging.debug(f"向上搜索帖子头失败: {e}")

    return None


async def _get_dest_targets(client, src_channel_id, src_post_id, forward):
    dest_targets = {}
    for dest_ch in forward.dest:
        dest_resolved = dest_ch
        if not isinstance(dest_resolved, int):
            try:
                dest_resolved = await config.get_id(client, dest_ch)
            except Exception:
                continue
        dest_post_id = st.get_dest_post_id(src_channel_id, src_post_id, dest_resolved)
        if not dest_post_id:
            logging.debug(f"未找到帖子映射: {src_channel_id}/{src_post_id} -> {dest_resolved}")
            continue
        if forward.comments.dest_mode == "comments":
            disc_msg = await get_discussion_message(client, dest_resolved, dest_post_id)
            if disc_msg:
                dest_targets[disc_msg.chat_id] = disc_msg.id
            else:
                dest_targets[dest_resolved] = dest_post_id
        else:
            for dg in forward.comments.dest_discussion_groups:
                try:
                    dg_id = await config.get_id(client, dg) if not isinstance(dg, int) else dg
                    dest_targets[dg_id] = None
                except Exception:
                    continue
    return dest_targets


async def _send_single_comment(client, message, dest_targets, chat_id):
    tm = await apply_plugins(message)
    if not tm:
        return
    try:
        for dest_chat_id, dest_reply_to in dest_targets.items():
            for attempt in range(3):
                try:
                    fwded = await send_message(dest_chat_id, tm, comment_to_post=dest_reply_to)
                    if fwded:
                        st.add_comment_mapping(chat_id, message.id, dest_chat_id, extract_msg_id(fwded))
                        break
                except Exception as e:
                    err_str = str(e).upper()
                    if "FLOOD" in err_str:
                        import re
                        wait_match = re.search(r"\d+", str(e))
                        wait = int(wait_match.group()) + 5 if wait_match else 30
                        await asyncio.sleep(wait)
                        continue
                    logging.error(f"评论发送失败 ({attempt+1}/3): {e}")
                    if attempt < 2:
                        await asyncio.sleep(3 * (attempt + 1))
    finally:
        tm.clear()


async def _send_grouped_comments(client, messages, dest_targets, chat_id):
    if not messages:
        return
    tms = await apply_plugins_to_group(messages)
    if not tms:
        return
    aligned_messages = _align_messages_and_tms(messages, tms)
    try:
        for dest_chat_id, dest_reply_to in dest_targets.items():
            for attempt in range(3):
                try:
                    fwded = await send_message(
                        dest_chat_id, tms[0],
                        grouped_messages=aligned_messages,
                        grouped_tms=tms,
                        comment_to_post=dest_reply_to,
                    )
                    if fwded:
                        fid = extract_msg_id(fwded[0] if isinstance(fwded, list) else fwded)
                        st.add_comment_mapping(chat_id, messages[0].id, dest_chat_id, fid)
                        break
                except Exception as e:
                    err_str = str(e).upper()
                    if "FLOOD" in err_str:
                        import re
                        wait_match = re.search(r"\d+", str(e))
                        wait = int(wait_match.group()) + 5 if wait_match else 30
                        await asyncio.sleep(wait)
                        continue
                    logging.error(f"评论媒体组发送失败 ({attempt+1}/3): {e}")
                    if attempt < 2:
                        await asyncio.sleep(3 * (attempt + 1))
    finally:
        for tm in tms:
            tm.clear()


async def _flush_comment_group(grouped_id):
    if grouped_id not in COMMENT_GROUPED_CACHE:
        return
    try:
        chat_messages_map = COMMENT_GROUPED_CACHE[grouped_id]
        for chat_id, messages in chat_messages_map.items():
            if chat_id not in config.comment_sources:
                continue
            forward = config.comment_forward_map.get(chat_id)
            if not forward or not forward.comments.enabled:
                continue
            src_post_id = await _resolve_channel_post_id(messages[0].client, chat_id, messages[0])
            if not src_post_id:
                logging.warning(f"评论媒体组无法解析帖子: {chat_id}")
                continue
            src_channel_id = config.comment_sources[chat_id]
            dest_targets = await _get_dest_targets(messages[0].client, src_channel_id, src_post_id, forward)
            if dest_targets:
                await _send_grouped_comments(messages[0].client, messages, dest_targets, chat_id)
    except Exception as e:
        logging.exception(f"处理评论媒体组失败: {e}")
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


async def comment_message_handler(event):
    chat_id = event.chat_id
    message = event.message
    if chat_id not in config.comment_sources:
        return
    forward = config.comment_forward_map.get(chat_id)
    if not forward or not forward.comments.enabled:
        return
    cp = _extract_channel_post(message)
    if cp:
        st.add_discussion_mapping(chat_id, message.id, cp)
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
    if message.grouped_id is not None:
        _add_comment_to_group_cache(chat_id, message.grouped_id, message)
        return
    src_post_id = await _resolve_channel_post_id(event.client, chat_id, message)
    if not src_post_id:
        logging.warning(f"无法解析评论帖子: {chat_id}/{message.id}")
        return
    src_channel_id = config.comment_sources[chat_id]
    dest_targets = await _get_dest_targets(event.client, src_channel_id, src_post_id, forward)
    if not dest_targets:
        logging.debug(f"评论无目标: {chat_id}/{message.id} -> post {src_post_id}")
        return
    await _send_single_comment(event.client, message, dest_targets, chat_id)


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
                logging.error(f"无法解析评论源: {forward.source}")
                continue
        if forward.comments.source_mode == "discussion":
            dg = forward.comments.source_discussion_group
            if dg is None:
                logging.warning(f"讨论组未设置: {forward.source}")
                continue
            if not isinstance(dg, int):
                try:
                    dg = await config.get_id(client, dg)
                except Exception:
                    logging.error(f"无法解析讨论组: {forward.comments.source_discussion_group}")
                    continue
            comment_sources[dg] = src
            comment_forward_map[dg] = forward
            logging.info(f"评论监听(手动): {dg} -> {src}")
        else:
            dg_id = await get_discussion_group_id(client, src)
            if dg_id is None:
                logging.warning(f"无法获取讨论组: {src}")
                continue
            comment_sources[dg_id] = src
            comment_forward_map[dg_id] = forward
            logging.info(f"评论监听(自动): {dg_id} -> {src}")
    return comment_sources, comment_forward_map


async def start_sync():
    clean_session_files()
    await load_async_plugins()
    SESSION = get_SESSION()
    client = TelegramClient(
        SESSION, CONFIG.login.API_ID, CONFIG.login.API_HASH,
        sequential_updates=CONFIG.live.sequential_updates,
    )
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
        logging.error("没有有效的转发配置")
        return
    logging.info(f"转发配置: {config.from_to}")
    has_comments = any(f.use_this and f.comments.enabled for f in CONFIG.forwards)
    if has_comments:
        cs, cf = await _setup_comment_listeners(client)
        config.comment_sources = cs
        config.comment_forward_map = cf
        if cs:
            for discussion_id in cs:
                count = await preload_discussion_mappings(client, discussion_id, limit=500)
                logging.info(f"预加载 {count} 个映射: {discussion_id}")
            client.add_event_handler(
                comment_message_handler,
                events.NewMessage(chats=list(cs.keys())),
            )
            logging.info(f"评论监听已启动: {list(cs.keys())}")
        else:
            logging.warning("未找到讨论组")
    for key, val in ALL_EVENTS.items():
        if not CONFIG.live.delete_sync and key == "deleted":
            continue
        client.add_event_handler(*val)
    logging.info("live模式启动完成")
    await client.run_until_disconnected()
