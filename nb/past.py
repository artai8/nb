import asyncio
import logging
import random
import time
from collections import defaultdict
from typing import Dict, List, Optional
from telethon import TelegramClient
from telethon.errors.rpcerrorlist import FloodWaitError
from telethon.tl.custom.message import Message
from telethon.tl.patched import MessageService
from nb import config
from nb import storage as st
from nb.config import CONFIG, get_SESSION, write_config
from nb.plugins import apply_plugins, apply_plugins_to_group, load_async_plugins
from nb.utils import (
    clean_session_files, extract_msg_id, send_message,
    _get_reply_to_msg_id, _get_reply_to_top_id, _extract_channel_post,
    get_discussion_message, get_discussion_group_id,
    walk_to_header, preload_discussion_mappings,
    COMMENT_MAX_RETRIES, COMMENT_RETRY_BASE_DELAY, wait_for_dest_post_id
)


async def _get_comments_method_a(client, channel_id, msg_id):
    try:
        comments = []
        async for msg in client.iter_messages(channel_id, reply_to=msg_id, reverse=True):
            comments.append(msg)
        return comments
    except Exception:
        return []


async def _get_comments_method_b(client, channel_id, msg_id):
    try:
        disc_msg = await get_discussion_message(client, channel_id, msg_id)
        if disc_msg is None:
            return []
        st.add_discussion_mapping(disc_msg.chat_id, disc_msg.id, msg_id)
        comments = []
        async for msg in client.iter_messages(disc_msg.chat_id, reply_to=disc_msg.id, reverse=True):
            comments.append(msg)
        return comments
    except Exception:
        return []


async def _get_comments_method_c(client, channel_id, msg_id):
    try:
        disc_msg = await get_discussion_message(client, channel_id, msg_id)
        if disc_msg is None:
            return []
        discussion_id = disc_msg.chat_id
        top_id = disc_msg.id
        st.add_discussion_mapping(discussion_id, top_id, msg_id)
        comments = []
        async for msg in client.iter_messages(discussion_id, min_id=top_id, reverse=True, limit=500):
            if msg.id == top_id:
                continue
            rt = getattr(msg, 'reply_to', None)
            if rt is None:
                continue
            if getattr(rt, 'reply_to_top_id', None) == top_id or getattr(rt, 'reply_to_msg_id', None) == top_id:
                comments.append(msg)
        return comments
    except Exception:
        return []


async def _get_comments_method_d(client, channel_id, msg_id):
    try:
        dg_id = await get_discussion_group_id(client, channel_id)
        if dg_id is None:
            return []
        disc_msg_id = st.get_discussion_msg_id(dg_id, msg_id)
        if disc_msg_id:
            comments = []
            async for msg in client.iter_messages(dg_id, reply_to=disc_msg_id, reverse=True):
                comments.append(msg)
            if comments:
                return comments
        async for msg in client.iter_messages(dg_id, limit=300):
            cp = _extract_channel_post(msg)
            if cp:
                st.add_discussion_mapping(dg_id, msg.id, cp)
                if cp == msg_id:
                    comments = []
                    async for reply in client.iter_messages(dg_id, reply_to=msg.id, reverse=True):
                        comments.append(reply)
                    return comments
        return []
    except Exception:
        return []


async def _get_all_comments(client, channel_id, msg_id, retry_delay=3):
    methods = [
        lambda: _get_comments_method_a(client, channel_id, msg_id),
        lambda: _get_comments_method_b(client, channel_id, msg_id),
        lambda: _get_comments_method_c(client, channel_id, msg_id),
        lambda: _get_comments_method_d(client, channel_id, msg_id),
    ]
    for attempt in range(COMMENT_MAX_RETRIES):
        for method in methods:
            try:
                comments = await method()
                if comments:
                    return comments
            except Exception:
                pass
        if attempt < COMMENT_MAX_RETRIES - 1:
            await asyncio.sleep(retry_delay * (attempt + 1))
    return []


def _group_comments(comments):
    units = []
    group_index = {}
    for msg in comments:
        gid = getattr(msg, 'grouped_id', None)
        if gid is None:
            units.append([msg])
        elif gid in group_index:
            units[group_index[gid]].append(msg)
        else:
            group_index[gid] = len(units)
            units.append([msg])
    return units


async def _send_past_grouped(client, src, dest, messages):
    tms = await apply_plugins_to_group(messages)
    if not tms or tms[0] is None:
        return False
    for d in dest:
        try:
            fwded = await send_message(d, tms[0], grouped_messages=[tm.message for tm in tms], grouped_tms=tms)
            uid = st.EventUid(st.DummyEvent(src, messages[0].id))
            st.stored[uid] = {d: fwded}
            fid = extract_msg_id(fwded)
            if fid is not None:
                st.add_post_mapping(src, messages[0].id, d, fid)
        except Exception as e:
            logging.critical(f"组播失败: {e}")
    return True


async def _flush_grouped_buffer(client, src, dest, grouped_buffer, forward):
    last_id = 0
    for gid, msgs in list(grouped_buffer.items()):
        if not msgs:
            continue
        await _send_past_grouped(client, src, dest, msgs)
        group_last_id = max(m.id for m in msgs)
        last_id = max(last_id, group_last_id)
        forward.offset = group_last_id
        write_config(CONFIG, persist=False)
        await asyncio.sleep(random.randint(60, 300))
    grouped_buffer.clear()
    return last_id


async def _send_comment_with_retry(client, dest_targets, tm=None, tms=None, grouped_messages=None, src_id=0, src_chat=0):
    for dest_chat_id, dest_reply_to in dest_targets.items():
        for attempt in range(COMMENT_MAX_RETRIES):
            try:
                if tms and grouped_messages:
                    fwded = await send_message(dest_chat_id, tms[0], grouped_messages=grouped_messages, grouped_tms=tms, comment_to_post=dest_reply_to)
                else:
                    fwded = await send_message(dest_chat_id, tm, comment_to_post=dest_reply_to)
                if fwded:
                    st.add_comment_mapping(src_chat, src_id, dest_chat_id, extract_msg_id(fwded))
                break
            except FloodWaitError as fwe:
                await asyncio.sleep(fwe.seconds + 5)
            except Exception as e:
                if attempt < COMMENT_MAX_RETRIES - 1:
                    await asyncio.sleep(COMMENT_RETRY_BASE_DELAY * (attempt + 1))
                else:
                    logging.error(f"评论发送最终失败 dest={dest_chat_id} src_msg={src_id}: {e}")


async def _resolve_dest_targets(client, src_channel_id, src_post_id, dest_list, cfg):
    dest_targets = {}
    for dest_ch in dest_list:
        dest_resolved = dest_ch
        if not isinstance(dest_resolved, int):
            try:
                dest_resolved = await config.get_id(client, dest_ch)
            except Exception:
                continue
        # === 关键修改：等待映射建立 ===
        dest_post_id = await wait_for_dest_post_id(src_channel_id, src_post_id, dest_resolved, timeout=120)
        if dest_post_id is None:
            logging.warning(f"Past模式：跳过评论，因目标帖子未转发 src_post={src_post_id} dest={dest_resolved}")
            continue
        if cfg.dest_mode == "comments":
            for attempt in range(COMMENT_MAX_RETRIES):
                try:
                    disc = await get_discussion_message(client, dest_resolved, dest_post_id)
                    if disc:
                        dest_targets[disc.chat_id] = disc.id
                    else:
                        dest_targets[dest_resolved] = dest_post_id
                    break
                except Exception:
                    if attempt < COMMENT_MAX_RETRIES - 1:
                        await asyncio.sleep(COMMENT_RETRY_BASE_DELAY * (attempt + 1))
                    else:
                        dest_targets[dest_resolved] = dest_post_id
        elif cfg.dest_mode == "discussion":
            for dg in cfg.dest_discussion_groups:
                dg_id = dg
                if not isinstance(dg_id, int):
                    try:
                        dg_id = await config.get_id(client, dg)
                    except Exception:
                        continue
                dest_targets[dg_id] = None
    return dest_targets


async def _forward_comments_for_post(client, src_channel_id, src_post_id, dest_list, forward):
    cfg = forward.comments
    await asyncio.sleep(3)
    comments = await _get_all_comments(client, src_channel_id, src_post_id, retry_delay=5)
    if not comments:
        return
    filtered = []
    for c in comments:
        if isinstance(c, MessageService):
            continue
        if _extract_channel_post(c):
            continue
        if cfg.only_media and not c.media:
            continue
        if not cfg.include_text_comments and not c.media:
            continue
        if cfg.skip_bot_comments:
            try:
                s = await c.get_sender()
                if s and getattr(s, 'bot', False):
                    continue
            except Exception:
                pass
        filtered.append(c)
    if not filtered:
        return
    units = _group_comments(filtered)
    dest_targets = await _resolve_dest_targets(client, src_channel_id, src_post_id, dest_list, cfg)
    if not dest_targets:
        return
    for unit_msgs in units:
        if len(unit_msgs) > 1:
            tms = await apply_plugins_to_group(unit_msgs)
            if not tms or tms[0] is None:
                continue
            await _send_comment_with_retry(
                client, dest_targets,
                tms=tms, grouped_messages=[tm.message for tm in tms],
                src_id=unit_msgs[0].id, src_chat=src_channel_id,
            )
            for tm in tms:
                tm.clear()
        else:
            comment = unit_msgs[0]
            tm = await apply_plugins(comment)
            if not tm:
                continue
            await _send_comment_with_retry(
                client, dest_targets,
                tm=tm, src_id=comment.id, src_chat=src_channel_id,
            )
            tm.clear()
        await asyncio.sleep(random.randint(5, 20))


async def forward_job():
    clean_session_files()
    await load_async_plugins()
    if CONFIG.login.user_type != 1:
        return
    if not CONFIG.login.SESSION_STRING:
        return
    SESSION = get_SESSION()
    async with TelegramClient(SESSION, CONFIG.login.API_ID, CONFIG.login.API_HASH) as client:
        if await client.is_bot():
            return
        config.from_to = await config.load_from_to(client, CONFIG.forwards)
        if not config.from_to:
            return
        resolved_forwards: Dict[int, config.Forward] = {}
        for forward in CONFIG.forwards:
            if not forward.use_this:
                continue
            if not isinstance(forward.source, int) and str(forward.source).strip() == "":
                continue
            try:
                resolved_forwards[await config.get_id(client, forward.source)] = forward
            except Exception:
                continue
        for src, dest in config.from_to.items():
            forward = resolved_forwards.get(src)
            if forward is None:
                continue
            if forward.comments.enabled:
                try:
                    dg_id = await get_discussion_group_id(client, src)
                    if dg_id:
                        count = await preload_discussion_mappings(client, dg_id, limit=2000)
                        logging.info(f"past模式预加载 {count} 个帖子映射: 讨论组={dg_id}")
                except Exception as e:
                    logging.warning(f"past模式预加载映射失败: {e}")
            grouped_buffer: Dict[int, List[Message]] = defaultdict(list)
            async for message in client.iter_messages(src, reverse=True, offset_id=forward.offset):
                if isinstance(message, MessageService):
                    continue
                if forward.end and message.id > forward.end:
                    break
                try:
                    current_gid = message.grouped_id
                    if grouped_buffer and (current_gid is None or current_gid not in grouped_buffer):
                        try:
                            await _flush_grouped_buffer(client, src, dest, grouped_buffer, forward)
                        except FloodWaitError as fwe:
                            await asyncio.sleep(fwe.seconds)
                            await _flush_grouped_buffer(client, src, dest, grouped_buffer, forward)
                    if current_gid is not None:
                        grouped_buffer[current_gid].append(message)
                        continue
                    tm = await apply_plugins(message)
                    if not tm:
                        continue
                    uid = st.EventUid(st.DummyEvent(message.chat_id, message.id))
                    st.stored[uid] = {}
                    for d in dest:
                        reply_to_id = None
                        if message.is_reply:
                            rmid = _get_reply_to_msg_id(message)
                            if rmid is not None:
                                r_uid = st.EventUid(st.DummyEvent(message.chat_id, rmid))
                                if r_uid in st.stored:
                                    fr = st.stored[r_uid].get(d)
                                    reply_to_id = fr if isinstance(fr, int) else getattr(fr, 'id', None) if fr else None
                        tm.reply_to = reply_to_id
                        try:
                            fwded = await send_message(d, tm)
                            if fwded is not None:
                                st.stored[uid][d] = fwded
                                fid = extract_msg_id(fwded)
                                if fid is not None:
                                    st.add_post_mapping(src, message.id, d, fid)
                        except Exception as e:
                            logging.error(f"发送失败: {e}")
                    tm.clear()
                    forward.offset = message.id
                    write_config(CONFIG, persist=False)
                    if forward.comments.enabled:
                        try:
                            await _forward_comments_for_post(client, src, message.id, dest, forward)
                        except Exception as e:
                            logging.error(f"帖子 {message.id} 评论失败: {e}")
                    await asyncio.sleep(random.randint(60, 300))
                except FloodWaitError as fwe:
                    await asyncio.sleep(fwe.seconds)
                except Exception as err:
                    logging.exception(err)
            if grouped_buffer:
                try:
                    await _flush_grouped_buffer(client, src, dest, grouped_buffer, forward)
                except Exception as e:
                    logging.exception(f"刷新剩余组失败: {e}")
        logging.info("past模式完成")
