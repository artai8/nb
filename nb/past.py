import asyncio
import logging
import random
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
    _get_reply_to_msg_id, _extract_channel_post,
    get_discussion_message, get_discussion_group_id,
    preload_discussion_mappings,
)


async def _get_all_comments(client, channel_id, post_id):
    comments = []
    try:
        disc_msg = await get_discussion_message(client, channel_id, post_id)
        if disc_msg:
            async for msg in client.iter_messages(
                disc_msg.chat_id, reply_to=disc_msg.id, reverse=True
            ):
                if not isinstance(msg, MessageService):
                    comments.append(msg)
            if comments:
                return comments
    except Exception:
        pass
    try:
        dg_id = await get_discussion_group_id(client, channel_id)
        if dg_id:
            header_id = None
            async for msg in client.iter_messages(dg_id, limit=200):
                cp = _extract_channel_post(msg)
                if cp == post_id:
                    header_id = msg.id
                    st.add_discussion_mapping(dg_id, msg.id, cp)
                    break
            if header_id:
                async for msg in client.iter_messages(
                    dg_id, reply_to=header_id, reverse=True
                ):
                    if not isinstance(msg, MessageService):
                        comments.append(msg)
                if comments:
                    return comments
    except Exception:
        pass
    try:
        async for msg in client.iter_messages(
            channel_id, reply_to=post_id, reverse=True
        ):
            if not isinstance(msg, MessageService):
                comments.append(msg)
        if comments:
            return comments
    except Exception:
        pass
    return []


def _group_comments_by_media(comments):
    units = []
    group_map = {}
    for msg in comments:
        gid = getattr(msg, 'grouped_id', None)
        if gid is None:
            units.append([msg])
        elif gid in group_map:
            units[group_map[gid]].append(msg)
        else:
            group_map[gid] = len(units)
            units.append([msg])
    return units


async def _forward_comments_for_post(client, src_channel_id, src_post_id, dest_list, forward):
    cfg = forward.comments
    await asyncio.sleep(2)
    comments = await _get_all_comments(client, src_channel_id, src_post_id)
    if not comments:
        return 0
    filtered = []
    for c in comments:
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
        return 0
    logging.info(f"帖子 {src_post_id}: {len(filtered)} 条评论")
    dest_targets = {}
    for dest_ch in dest_list:
        dest_resolved = dest_ch
        if not isinstance(dest_resolved, int):
            try:
                dest_resolved = await config.get_id(client, dest_ch)
            except Exception:
                continue
        dest_post_id = st.get_dest_post_id(src_channel_id, src_post_id, dest_resolved)
        if not dest_post_id:
            continue
        if cfg.dest_mode == "comments":
            disc_msg = await get_discussion_message(client, dest_resolved, dest_post_id)
            if disc_msg:
                dest_targets[disc_msg.chat_id] = disc_msg.id
            else:
                dest_targets[dest_resolved] = dest_post_id
        else:
            for dg in cfg.dest_discussion_groups:
                try:
                    dg_id = await config.get_id(client, dg) if not isinstance(dg, int) else dg
                    dest_targets[dg_id] = None
                except Exception:
                    continue
    if not dest_targets:
        return 0
    units = _group_comments_by_media(filtered)
    forwarded = 0
    for unit in units:
        if len(unit) > 1:
            tms = await apply_plugins_to_group(unit)
            if not tms or not tms[0]:
                continue
            for dest_chat_id, dest_reply_to in dest_targets.items():
                try:
                    fwded = await send_message(
                        dest_chat_id, tms[0],
                        grouped_messages=[tm.message for tm in tms],
                        grouped_tms=tms,
                        comment_to_post=dest_reply_to,
                    )
                    if fwded:
                        st.add_comment_mapping(src_channel_id, unit[0].id, dest_chat_id, extract_msg_id(fwded))
                        forwarded += 1
                except Exception as e:
                    logging.error(f"评论媒体组失败: {e}")
            for tm in tms:
                tm.clear()
        else:
            comment = unit[0]
            tm = await apply_plugins(comment)
            if not tm:
                continue
            for dest_chat_id, dest_reply_to in dest_targets.items():
                try:
                    fwded = await send_message(dest_chat_id, tm, comment_to_post=dest_reply_to)
                    if fwded:
                        st.add_comment_mapping(src_channel_id, comment.id, dest_chat_id, extract_msg_id(fwded))
                        forwarded += 1
                except Exception as e:
                    logging.error(f"评论失败: {e}")
            tm.clear()
        await asyncio.sleep(random.uniform(1, 3))
    return forwarded


async def forward_job():
    logging.info("启动 Past 模式")
    clean_session_files()
    await load_async_plugins()
    if CONFIG.login.user_type != 1:
        logging.error("Past模式需要User账号")
        return
    if not CONFIG.login.SESSION_STRING:
        logging.error("SESSION_STRING未设置")
        return
    SESSION = get_SESSION()
    async with TelegramClient(SESSION, CONFIG.login.API_ID, CONFIG.login.API_HASH) as client:
        if await client.is_bot():
            logging.error("Bot不支持Past模式")
            return
        config.from_to = await config.load_from_to(client, CONFIG.forwards)
        if not config.from_to:
            logging.error("没有有效转发配置")
            return
        resolved_forwards: Dict[int, config.Forward] = {}
        for forward in CONFIG.forwards:
            if not forward.use_this:
                continue
            try:
                src_id = await config.get_id(client, forward.source)
                resolved_forwards[src_id] = forward
            except Exception:
                continue
        for src, dest in config.from_to.items():
            forward = resolved_forwards.get(src)
            if not forward:
                continue
            logging.info(f"处理频道: {src} -> {dest}")
            if forward.comments.enabled:
                try:
                    dg_id = await get_discussion_group_id(client, src)
                    if dg_id:
                        count = await preload_discussion_mappings(client, dg_id, limit=1000)
                        logging.info(f"预加载 {count} 个讨论映射")
                except Exception as e:
                    logging.warning(f"预加载失败: {e}")
            grouped_buffer: Dict[int, List[Message]] = defaultdict(list)
            post_count = 0
            comment_count = 0
            async for message in client.iter_messages(src, reverse=True, offset_id=forward.offset):
                if isinstance(message, MessageService):
                    continue
                if forward.end and message.id > forward.end:
                    break
                try:
                    current_gid = message.grouped_id
                    if grouped_buffer and (current_gid is None or current_gid not in grouped_buffer):
                        for gid, msgs in list(grouped_buffer.items()):
                            if not msgs:
                                continue
                            tms = await apply_plugins_to_group(msgs)
                            if tms and tms[0]:
                                for d in dest:
                                    try:
                                        fwded = await send_message(
                                            d, tms[0],
                                            grouped_messages=[tm.message for tm in tms],
                                            grouped_tms=tms,
                                        )
                                        if fwded:
                                            uid = st.EventUid(st.DummyEvent(src, msgs[0].id))
                                            st.stored[uid] = {d: fwded}
                                            fid = extract_msg_id(fwded[0] if isinstance(fwded, list) else fwded)
                                            if fid:
                                                st.add_post_mapping(src, msgs[0].id, d, fid)
                                                post_count += 1
                                    except Exception as e:
                                        logging.error(f"媒体组失败: {e}")
                                for tm in tms:
                                    tm.clear()
                            if forward.comments.enabled:
                                cc = await _forward_comments_for_post(client, src, msgs[0].id, dest, forward)
                                comment_count += cc
                            forward.offset = max(m.id for m in msgs)
                            write_config(CONFIG, persist=False)
                            await asyncio.sleep(random.randint(30, 120))
                        grouped_buffer.clear()
                    if current_gid is not None:
                        grouped_buffer[current_gid].append(message)
                        continue
                    logging.info(f"处理消息: {message.id}")
                    tm = await apply_plugins(message)
                    if not tm:
                        continue
                    uid = st.EventUid(st.DummyEvent(message.chat_id, message.id))
                    st.stored[uid] = {}
                    for d in dest:
                        reply_to_id = None
                        if message.is_reply:
                            rmid = _get_reply_to_msg_id(message)
                            if rmid:
                                r_uid = st.EventUid(st.DummyEvent(message.chat_id, rmid))
                                if r_uid in st.stored:
                                    fr = st.stored[r_uid].get(d)
                                    reply_to_id = (
                                        fr if isinstance(fr, int)
                                        else getattr(fr, 'id', None) if fr
                                        else None
                                    )
                        tm.reply_to = reply_to_id
                        try:
                            fwded = await send_message(d, tm)
                            if fwded:
                                st.stored[uid][d] = fwded
                                fid = extract_msg_id(fwded)
                                if fid:
                                    st.add_post_mapping(src, message.id, d, fid)
                        except Exception as e:
                            logging.error(f"发送失败: {e}")
                    tm.clear()
                    post_count += 1
                    forward.offset = message.id
                    write_config(CONFIG, persist=False)
                    if forward.comments.enabled:
                        cc = await _forward_comments_for_post(client, src, message.id, dest, forward)
                        comment_count += cc
                    await asyncio.sleep(random.randint(30, 120))
                except FloodWaitError as fwe:
                    logging.warning(f"FloodWait: {fwe.seconds}秒")
                    await asyncio.sleep(fwe.seconds + 10)
                except Exception as e:
                    logging.exception(f"处理消息出错: {e}")
            for gid, msgs in grouped_buffer.items():
                if not msgs:
                    continue
                tms = await apply_plugins_to_group(msgs)
                if tms and tms[0]:
                    for d in dest:
                        try:
                            fwded = await send_message(
                                d, tms[0],
                                grouped_messages=[tm.message for tm in tms],
                                grouped_tms=tms,
                            )
                            if fwded:
                                fid = extract_msg_id(fwded[0] if isinstance(fwded, list) else fwded)
                                if fid:
                                    st.add_post_mapping(src, msgs[0].id, d, fid)
                                    post_count += 1
                        except Exception as e:
                            logging.error(f"剩余媒体组失败: {e}")
                    for tm in tms:
                        tm.clear()
                if forward.comments.enabled:
                    cc = await _forward_comments_for_post(client, src, msgs[0].id, dest, forward)
                    comment_count += cc
            logging.info(f"频道 {src} 完成: 帖子={post_count}, 评论={comment_count}")
        logging.info("Past模式完成")
