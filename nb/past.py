# nb/past.py

import asyncio
import logging
import random
from collections import defaultdict
from typing import List, Dict, Optional

from telethon import TelegramClient
from telethon.errors.rpcerrorlist import FloodWaitError
from telethon.tl.custom.message import Message
from telethon.tl.patched import MessageService

from nb import config
from nb import storage as st
from nb.config import CONFIG, get_SESSION, write_config
from nb.plugins import apply_plugins, apply_plugins_to_group, load_async_plugins
from nb.utils import clean_session_files, send_message, _get_reply_to_msg_id, _extract_msg_id


async def _send_past_grouped(
    client, src, dest, messages, forward_cfg, 
    is_comm=False, is_first=False, is_last=False
):
    tms = await apply_plugins_to_group(messages, is_comm, is_first, is_last)
    if not tms: return False
    
    tm_template = tms[0]
    for d in dest:
        # 寻找评论的回复目标
        reply_to_id = None
        if is_comm:
            parent_uid = st.EventUid(st.DummyEvent(src, messages[0].reply_to_msg_id))
            if parent_uid in st.stored:
                fwded_parent = st.stored[parent_uid].get(d)
                reply_to_id = _extract_msg_id(fwded_parent)
        else:
            reply_to_id = tm_template.reply_to

        tm_template.reply_to = reply_to_id
        try:
            fwded_msgs = await send_message(
                d, tm_template, 
                grouped_messages=[tm.message for tm in tms], 
                grouped_tms=tms
            )
            event_uid = st.EventUid(st.DummyEvent(src, messages[0].id))
            if event_uid not in st.stored: st.stored[event_uid] = {}
            st.stored[event_uid][d] = fwded_msgs
        except Exception as e:
            logging.error(f"🚨 组播失败: {e}")
    return True


async def _process_replies(client, src, dest, parent_id, forward_cfg):
    """处理并转发某条消息的评论区"""
    logging.info(f"🔎 抓取消息 {parent_id} 的评论...")
    replies_pool = []
    text_count = 0
    
    async for r in client.iter_messages(src, reply_to=parent_id, reverse=True):
        if isinstance(r, MessageService): continue
        is_media = bool(r.media)
        if not is_media:
            if forward_cfg.comm_only_media: continue
            if text_count >= forward_cfg.comm_max_text: continue
            text_count += 1
        replies_pool.append(r)
    
    if not replies_pool: return
    
    total = len(replies_pool)
    grouped_buffer = defaultdict(list)
    
    for i, reply in enumerate(replies_pool):
        is_first = (i == 0)
        is_last = (i == total - 1)
        
        gid = reply.grouped_id
        if grouped_buffer and (gid is None or gid not in grouped_buffer):
            await _send_past_grouped(client, src, dest, list(grouped_buffer.values())[0], forward_cfg, True, is_first, is_last)
            grouped_buffer.clear()
            await asyncio.sleep(random.randint(2, 5))

        if gid is not None:
            grouped_buffer[gid].append(reply)
            continue
            
        # 单条处理
        tm = await apply_plugins(reply, is_comment=True, is_first=is_first, is_last=is_last)
        if not tm: continue
        
        event_uid = st.EventUid(st.DummyEvent(src, reply.id))
        st.stored[event_uid] = {}
        
        for d in dest:
            parent_uid = st.EventUid(st.DummyEvent(src, parent_id))
            reply_to_id = None
            if parent_uid in st.stored:
                fwded_parent = st.stored[parent_uid].get(d)
                reply_to_id = _extract_msg_id(fwded_parent)
            
            tm.reply_to = reply_to_id
            try:
                fwded = await send_message(d, tm)
                st.stored[event_uid][d] = fwded
            except: pass
        
        tm.clear()
        await asyncio.sleep(random.randint(1, 3))

    if grouped_buffer:
        await _send_past_grouped(client, src, dest, list(grouped_buffer.values())[0], forward_cfg, True, False, True)


async def forward_job() -> None:
    clean_session_files()
    await load_async_plugins()
    if CONFIG.login.user_type != 1: return

    async with TelegramClient(get_SESSION(), CONFIG.login.API_ID, CONFIG.login.API_HASH) as client:
        config.from_to = await config.load_from_to(client, CONFIG.forwards)
        
        for from_to, forward in zip(config.from_to.items(), CONFIG.forwards):
            src, dest = from_to
            grouped_buffer = defaultdict(list)

            async for message in client.iter_messages(src, reverse=True, offset_id=forward.offset):
                if isinstance(message, MessageService): continue
                if forward.end and message.id > forward.end: break

                try:
                    gid = message.grouped_id
                    if grouped_buffer and (gid is None or gid not in grouped_buffer):
                        await _send_past_grouped(client, src, dest, list(grouped_buffer.values())[0], forward)
                        grouped_buffer.clear()
                        # 发送完组后，如果开启评论，处理组内第一条的评论
                        if forward.forward_comments:
                            await _process_replies(client, src, dest, message.id - 1, forward) 
                        await asyncio.sleep(random.randint(5, 15))

                    if gid is not None:
                        grouped_buffer[gid].append(message)
                        continue

                    # 单条消息
                    tm = await apply_plugins(message)
                    if tm:
                        event_uid = st.EventUid(st.DummyEvent(src, message.id))
                        st.stored[event_uid] = {}
                        for d in dest:
                            # 处理原生回复（如果是回复别人）
                            r_id = _get_reply_to_msg_id(message)
                            reply_to = None
                            if r_id:
                                r_uid = st.EventUid(st.DummyEvent(src, r_id))
                                if r_uid in st.stored:
                                    reply_to = _extract_msg_id(st.stored[r_uid].get(d))
                            tm.reply_to = reply_to
                            st.stored[event_uid][d] = await send_message(d, tm)
                        tm.clear()
                        
                        # 处理评论
                        if forward.forward_comments:
                            await _process_replies(client, src, dest, message.id, forward)

                    forward.offset = message.id
                    write_config(CONFIG, persist=False)
                    await asyncio.sleep(random.randint(10, 30))

                except FloodWaitError as fwe:
                    await asyncio.sleep(fwe.seconds)
                except Exception as e:
                    logging.exception(e)
