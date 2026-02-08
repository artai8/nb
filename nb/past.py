# nb/past.py —— 修复版：正确处理媒体组延迟、offset、以及连续组的刷新

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
from nb.utils import clean_session_files, send_message


async def _send_past_grouped(
    client: TelegramClient, src: int, dest: List[int], messages: List[Message]
) -> bool:
    """强制发送整组消息"""
    tms = await apply_plugins_to_group(messages)
    if not tms:
        logging.warning("⚠️ 所有消息被插件过滤，但仍尝试发送空相册...")
        tms = [await apply_plugins(messages[0])]

    tm_template = tms[0]

    for d in dest:
        try:
            fwded_msgs = await send_message(
                d,
                tm_template,
                grouped_messages=[tm.message for tm in tms],
                grouped_tms=tms
            )

            first_msg_id = messages[0].id
            event_uid = st.EventUid(st.DummyEvent(src, first_msg_id))
            st.stored[event_uid] = {d: fwded_msgs}

        except Exception as e:
            logging.critical(f"🚨 组播失败但将继续重试（不中断）: {e}")

    return True


async def _flush_grouped_buffer(
    client: TelegramClient,
    src: int,
    dest: List[int],
    grouped_buffer: Dict[int, List[Message]],
    forward,
) -> int:
    """
    刷新所有已缓存的媒体组，逐组发送并在每组之间 sleep。
    返回最后处理的消息 ID（用于更新 offset）。
    """
    last_id = 0
    for gid, msgs in list(grouped_buffer.items()):
        if not msgs:
            continue

        await _send_past_grouped(client, src, dest, msgs)

        # 取组内最大消息 ID 作为 offset
        group_last_id = max(m.id for m in msgs)
        last_id = max(last_id, group_last_id)

        # 更新 offset
        forward.offset = group_last_id
        write_config(CONFIG, persist=False)

        logging.info(f"✅ 媒体组 {gid} ({len(msgs)} 条) 发送完成, offset → {group_last_id}")

        # 每组之间也要延迟，防止 FloodWait
        delay_seconds = random.randint(60, 300)
        logging.info(f"⏸️ 媒体组发送后休息 {delay_seconds} 秒")
        await asyncio.sleep(delay_seconds)

    grouped_buffer.clear()
    return last_id


async def forward_job() -> None:
    clean_session_files()
    await load_async_plugins()

    if CONFIG.login.user_type != 1:
        logging.warning("⚠️ past 模式仅支持用户账号")
        return

    SESSION = get_SESSION()
    async with TelegramClient(SESSION, CONFIG.login.API_ID, CONFIG.login.API_HASH) as client:
        config.from_to = await config.load_from_to(client, CONFIG.forwards)

        for from_to, forward in zip(config.from_to.items(), CONFIG.forwards):
            src, dest = from_to
            last_id = 0
            grouped_buffer: Dict[int, List[Message]] = defaultdict(list)
            # 记录上一条消息的 grouped_id，用于检测组边界
            prev_grouped_id: Optional[int] = None

            async for message in client.iter_messages(src, reverse=True, offset_id=forward.offset):
                if isinstance(message, MessageService):
                    continue

                if forward.end and message.id > forward.end:
                    continue

                try:
                    current_grouped_id = message.grouped_id

                    # ── 检测组边界：当前消息不属于之前缓存的组 ──
                    # 情况1: 上一条是组消息，当前是单条消息 → 刷新
                    # 情况2: 上一条是组A，当前是组B → 刷新组A
                    # 情况3: 上一条是组消息，当前也是同组 → 继续缓存
                    if grouped_buffer and (
                        current_grouped_id is None  # 单条消息，刷新之前的组
                        or (current_grouped_id is not None
                            and current_grouped_id not in grouped_buffer)  # 新的组，刷新之前的
                    ):
                        try:
                            flushed_last = await _flush_grouped_buffer(
                                client, src, dest, grouped_buffer, forward
                            )
                            if flushed_last:
                                last_id = max(last_id, flushed_last)
                        except FloodWaitError as fwe:
                            logging.warning(f"⛔ FloodWait (组刷新): 等待 {fwe.seconds} 秒")
                            await asyncio.sleep(fwe.seconds)
                            # 重试刷新
                            flushed_last = await _flush_grouped_buffer(
                                client, src, dest, grouped_buffer, forward
                            )
                            if flushed_last:
                                last_id = max(last_id, flushed_last)

                    # ── 当前消息是媒体组的一部分 → 缓存 ──
                    if current_grouped_id is not None:
                        grouped_buffer[current_grouped_id].append(message)
                        prev_grouped_id = current_grouped_id
                        continue

                    # ── 处理单条消息 ──
                    prev_grouped_id = None

                    tm = await apply_plugins(message)
                    if not tm:
                        continue

                    event_uid = st.EventUid(st.DummyEvent(message.chat_id, message.id))
                    st.stored[event_uid] = {}

                    if message.is_reply:
                        r_event = st.DummyEvent(message.chat_id, message.reply_to_msg_id)
                        r_event_uid = st.EventUid(r_event)
                        if r_event_uid in st.stored:
                            tm.reply_to = st.stored[r_event_uid].get(dest[0])

                    for d in dest:
                        try:
                            fwded_msg = await send_message(d, tm)
                            st.stored[event_uid][d] = fwded_msg.id
                        except Exception as e:
                            logging.error(f"❌ 单条发送失败: {e}")

                    tm.clear()
                    last_id = message.id
                    forward.offset = last_id
                    write_config(CONFIG, persist=False)

                    delay_seconds = random.randint(60, 300)
                    logging.info(f"⏸️ 休息 {delay_seconds} 秒 (单条消息 {message.id})")
                    await asyncio.sleep(delay_seconds)

                except FloodWaitError as fwe:
                    logging.warning(f"⛔ FloodWait: 等待 {fwe.seconds} 秒")
                    await asyncio.sleep(fwe.seconds)
                except Exception as err:
                    logging.exception(err)

            # ── 循环结束后，刷新剩余的媒体组 ──
            if grouped_buffer:
                logging.info(f"📦 刷新剩余 {len(grouped_buffer)} 个媒体组")
                try:
                    await _flush_grouped_buffer(client, src, dest, grouped_buffer, forward)
                except Exception as e:
                    logging.exception(f"🚨 刷新剩余组失败: {e}")
