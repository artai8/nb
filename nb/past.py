# nb/past.py 完整代码

import asyncio
import logging
import random
from collections import defaultdict
from telethon import TelegramClient
from telethon.errors.rpcerrorlist import FloodWaitError
from telethon.tl.patched import MessageService
from nb import config, storage as st
from nb.config import CONFIG, get_SESSION, write_config
from nb.plugins import apply_plugins, apply_plugins_to_group, load_async_plugins
from nb.utils import clean_session_files, send_message, _get_reply_to_msg_id, _extract_msg_id

async def _send_past_grouped(client, src, dest, messages, forward_cfg, is_comm=False, is_first=False, is_last=False):
    tms = await apply_plugins_to_group(messages, is_comm, is_first, is_last)
    if not tms: return False
    tm_template = tms[0]
    for d in dest:
        reply_to_id = None
        if is_comm:
            parent_uid = st.EventUid(st.DummyEvent(src, messages[0].reply_to_msg_id))
            reply_to_id = _extract_msg_id(st.stored.get(parent_uid, {}).get(d))
        else:
            reply_to_id = tm_template.reply_to
        tm_template.reply_to = reply_to_id
        try:
            fwded = await send_message(d, tm_template, [tm.message for tm in tms], tms)
            uid = st.EventUid(st.DummyEvent(src, messages[0].id))
            if uid not in st.stored: st.stored[uid] = {}
            st.stored[uid][d] = fwded
        except Exception as e: logging.error(f"❌ 组播失败: {e}")
    return True

async def _process_replies(client, src, dest, parent_id, forward_cfg):
    logging.info(f"💬 正在抓取主贴 {parent_id} 的评论...")
    pool = []
    text_count = 0
    async for r in client.iter_messages(src, reply_to=parent_id, reverse=True):
        if isinstance(r, MessageService): continue
        if not r.media:
            if forward_cfg.comm_only_media or text_count >= forward_cfg.comm_max_text: continue
            text_count += 1
        pool.append(r)
    
    if not pool: return
    total = len(pool)
    grouped_buffer = defaultdict(list)
    for i, reply in enumerate(pool):
        is_first, is_last = (i == 0), (i == total - 1)
        gid = reply.grouped_id
        if grouped_buffer and (gid is None or gid not in grouped_buffer):
            await _send_past_grouped(client, src, dest, list(grouped_buffer.values())[0], forward_cfg, True, is_first, is_last)
            grouped_buffer.clear()
            await asyncio.sleep(2)
        if gid is not None:
            grouped_buffer[gid].append(reply)
            continue
        tm = await apply_plugins(reply, True, is_first, is_last)
        if not tm: continue
        uid = st.EventUid(st.DummyEvent(src, reply.id))
        st.stored[uid] = {}
        for d in dest:
            p_uid = st.EventUid(st.DummyEvent(src, parent_id))
            tm.reply_to = _extract_msg_id(st.stored.get(p_uid, {}).get(d))
            st.stored[uid][d] = await send_message(d, tm)
        tm.clear()
        await asyncio.sleep(1)
    if grouped_buffer:
        await _send_past_grouped(client, src, dest, list(grouped_buffer.values())[0], forward_cfg, True, False, True)

async def forward_job() -> None:
    logging.info("🚀 Past 模式任务启动...")
    clean_session_files()
    await load_async_plugins()
    if CONFIG.login.user_type != 1:
        logging.critical("❌ Past 模式仅支持 User 账号！请在登录页面切换。")
        return

    async with TelegramClient(get_SESSION(), CONFIG.login.API_ID, CONFIG.login.API_HASH) as client:
        config.from_to = await config.load_from_to(client, CONFIG.forwards)
        if not config.from_to:
            logging.warning("⚠️ 没有有效的转发任务，请检查 Connection 设置及 ID/链接是否正确。")
            return

        for src, dest in config.from_to.items():
            # 找到对应的 forward 配置
            forward = next((f for f in CONFIG.forwards if f.use_this), None)
            if not forward: continue
            
            logging.info(f"📂 开始处理源: {src} -> 目的地: {dest}")
            grouped_buffer = defaultdict(list)

            async for message in client.iter_messages(src, reverse=True, offset_id=forward.offset):
                if isinstance(message, MessageService): continue
                if forward.end and message.id > forward.end: break

                logging.info(f"📖 正在处理消息 ID: {message.id}")
                try:
                    gid = message.grouped_id
                    if grouped_buffer and (gid is None or gid not in grouped_buffer):
                        first_msg = list(grouped_buffer.values())[0][0]
                        await _send_past_grouped(client, src, dest, list(grouped_buffer.values())[0], forward)
                        grouped_buffer.clear()
                        if forward.forward_comments:
                            await _process_replies(client, src, dest, first_msg.id, forward)
                        await asyncio.sleep(forward.past.delay or 5)

                    if gid is not None:
                        grouped_buffer[gid].append(message)
                        continue

                    tm = await apply_plugins(message)
                    if tm:
                        uid = st.EventUid(st.DummyEvent(src, message.id))
                        st.stored[uid] = {}
                        for d in dest:
                            r_id = _get_reply_to_msg_id(message)
                            tm.reply_to = _extract_msg_id(st.stored.get(st.EventUid(st.DummyEvent(src, r_id)), {}).get(d)) if r_id else None
                            st.stored[uid][d] = await send_message(d, tm)
                        tm.clear()
                        if forward.forward_comments:
                            await _process_replies(client, src, dest, message.id, forward)

                    forward.offset = message.id
                    write_config(CONFIG, persist=False)
                    await asyncio.sleep(forward.past.delay or 5)

                except FloodWaitError as e:
                    logging.warning(f"⏳ 触发 FloodWait，等待 {e.seconds} 秒...")
                    await asyncio.sleep(e.seconds)
                except Exception as e:
                    logging.exception(f"❌ 运行异常: {e}")
