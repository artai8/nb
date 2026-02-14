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
#  评论区获取（多种方法 + 重试）
# =====================================================================


async def _get_comments_method_a(
    client: TelegramClient,
    channel_id,
    msg_id: int,
) -> List[Message]:
    """方法A: 直接 iter_messages(channel, reply_to=msg_id)。

    Telethon 会自动用 GetRepliesRequest。
    ★ 某些情况下可能返回空（帖子太新/评论未索引）。
    """
    comments = []
    try:
        async for msg in client.iter_messages(
            channel_id,
            reply_to=msg_id,
            reverse=True,
        ):
            comments.append(msg)

        if comments:
            logging.info(
                f"💬 方法A: 获取到 {len(comments)} 条评论 "
                f"(channel={channel_id}, post={msg_id})"
            )
    except Exception as e:
        logging.warning(
            f"⚠️ 方法A失败 (channel={channel_id}, post={msg_id}): {e}"
        )
    return comments


async def _get_comments_method_b(
    client: TelegramClient,
    channel_id,
    msg_id: int,
) -> List[Message]:
    """方法B: 通过 GetDiscussionMessage 找到讨论组，
    然后在讨论组中 iter_messages(reply_to=top_id)。
    """
    comments = []
    try:
        disc_msg = await get_discussion_message(client, channel_id, msg_id)
        if disc_msg is None:
            logging.info(f"💬 方法B: 帖子 {msg_id} 没有讨论消息")
            return comments

        discussion_id = disc_msg.chat_id
        top_id = disc_msg.id

        # 记录映射
        st.discussion_to_channel_post[(discussion_id, top_id)] = msg_id

        logging.info(
            f"💬 方法B: 讨论组={discussion_id}, top_id={top_id}, "
            f"正在遍历评论..."
        )

        async for msg in client.iter_messages(
            discussion_id,
            reply_to=top_id,
            reverse=True,
        ):
            comments.append(msg)

        if comments:
            logging.info(
                f"💬 方法B: 获取到 {len(comments)} 条评论"
            )
    except Exception as e:
        logging.warning(f"⚠️ 方法B失败: {e}")
    return comments


async def _get_comments_method_c(
    client: TelegramClient,
    channel_id,
    msg_id: int,
) -> List[Message]:
    """方法C: 获取讨论组后，遍历讨论组的全部近期消息，
    手动过滤出属于这个帖子的评论。

    ★ 这是最暴力但最可靠的方法，用于前两种方法都失败时。
    """
    comments = []
    try:
        disc_msg = await get_discussion_message(client, channel_id, msg_id)
        if disc_msg is None:
            return comments

        discussion_id = disc_msg.chat_id
        top_id = disc_msg.id

        st.discussion_to_channel_post[(discussion_id, top_id)] = msg_id

        logging.info(
            f"💬 方法C: 暴力扫描讨论组 {discussion_id} "
            f"(top_id={top_id})..."
        )

        # 从 top_id 之后开始遍历，获取之后的消息
        async for msg in client.iter_messages(
            discussion_id,
            min_id=top_id,
            reverse=True,
            limit=500,
        ):
            if msg.id == top_id:
                continue

            # 检查是否属于这个帖子的评论
            reply_to = getattr(msg, 'reply_to', None)
            if reply_to is None:
                continue

            msg_reply_to = getattr(reply_to, 'reply_to_msg_id', None)
            msg_top_id = getattr(reply_to, 'reply_to_top_id', None)

            if msg_top_id == top_id or msg_reply_to == top_id:
                comments.append(msg)

        if comments:
            logging.info(
                f"💬 方法C: 获取到 {len(comments)} 条评论"
            )
    except Exception as e:
        logging.warning(f"⚠️ 方法C失败: {e}")
    return comments


async def _get_all_comments(
    client: TelegramClient,
    channel_id,
    msg_id: int,
    retry_delay: int = 3,
) -> List[Message]:
    """尝试多种方法获取帖子评论，保证最大可靠性。

    顺序: A → B → 等待后重试A → C
    """
    # 第一轮: 方法A
    comments = await _get_comments_method_a(client, channel_id, msg_id)
    if comments:
        return comments

    # 第二轮: 方法B
    comments = await _get_comments_method_b(client, channel_id, msg_id)
    if comments:
        return comments

    # 第三轮: 等待后重试方法A（评论索引可能延迟）
    if retry_delay > 0:
        logging.info(
            f"💬 方法A/B均未获取到评论，等待 {retry_delay}s 后重试..."
        )
        await asyncio.sleep(retry_delay)
        comments = await _get_comments_method_a(client, channel_id, msg_id)
        if comments:
            return comments

    # 第四轮: 方法C（暴力扫描）
    comments = await _get_comments_method_c(client, channel_id, msg_id)
    return comments


# =====================================================================
#  评论区整理
# =====================================================================


def _group_comments(comments: List[Message]) -> List[List[Message]]:
    """将评论按 grouped_id 整理为发送单元。"""
    units: List[List[Message]] = []
    group_index: Dict[int, int] = {}

    for msg in comments:
        gid = getattr(msg, 'grouped_id', None)
        if gid is None:
            units.append([msg])
        else:
            if gid in group_index:
                units[group_index[gid]].append(msg)
            else:
                group_index[gid] = len(units)
                units.append([msg])

    return units


# =====================================================================
#  主帖子媒体组
# =====================================================================


async def _send_past_grouped(
    client: TelegramClient, src: int, dest: List[int], messages: List[Message]
) -> bool:
    tms = await apply_plugins_to_group(messages)
    if not tms:
        return False

    tm_template = tms[0]
    if tm_template is None:
        return False

    for d in dest:
        try:
            fwded_msgs = await send_message(
                d, tm_template,
                grouped_messages=[tm.message for tm in tms],
                grouped_tms=tms,
            )

            first_msg_id = messages[0].id
            event_uid = st.EventUid(st.DummyEvent(src, first_msg_id))
            st.stored[event_uid] = {d: fwded_msgs}

            fwded_id = _extract_msg_id(fwded_msgs)
            if fwded_id is not None:
                st.add_post_mapping(src, first_msg_id, d, fwded_id)

        except Exception as e:
            logging.critical(f"🚨 组播失败: {e}")

    return True


async def _flush_grouped_buffer(
    client: TelegramClient,
    src: int,
    dest: List[int],
    grouped_buffer: Dict[int, List[Message]],
    forward,
) -> int:
    last_id = 0
    for gid, msgs in list(grouped_buffer.items()):
        if not msgs:
            continue

        await _send_past_grouped(client, src, dest, msgs)

        group_last_id = max(m.id for m in msgs)
        last_id = max(last_id, group_last_id)

        forward.offset = group_last_id
        write_config(CONFIG, persist=False)

        delay_seconds = random.randint(60, 300)
        logging.info(
            f"✅ 媒体组 {gid} ({len(msgs)} 条) 完成, "
            f"offset → {group_last_id}, 休息 {delay_seconds}s"
        )
        await asyncio.sleep(delay_seconds)

    grouped_buffer.clear()
    return last_id


# =====================================================================
#  评论区转发（★ 全面修复版）
# =====================================================================


async def _forward_comments_for_post(
    client: TelegramClient,
    src_channel_id: int,
    src_post_id: int,
    dest_list: List[int],
    forward: config.Forward,
) -> None:
    """获取源帖子评论并转发到目标帖子评论区。

    修复点:
    1. 三种方法获取评论，确保可靠性
    2. 获取前短暂延迟，等待 Telegram 索引评论
    3. 媒体组整组发送
    4. 每步都有详细日志
    """
    comments_cfg = forward.comments

    logging.info(
        f"💬 ═══ 开始处理帖子 {src_post_id} 的评论 ═══"
    )

    # ★ 短暂延迟：让 Telegram 有时间索引评论
    await asyncio.sleep(2)

    # ========== 获取评论 ==========
    comments = await _get_all_comments(
        client, src_channel_id, src_post_id, retry_delay=5
    )

    if not comments:
        logging.info(f"💬 帖子 {src_post_id} 没有评论")
        return

    logging.info(f"💬 帖子 {src_post_id}: 原始评论 {len(comments)} 条")

    # ========== 预过滤 ==========
    filtered = []
    for comment in comments:
        if isinstance(comment, MessageService):
            continue

        # 跳过频道帖子副本
        if hasattr(comment, 'fwd_from') and comment.fwd_from:
            if getattr(comment.fwd_from, 'channel_post', None):
                continue

        if comments_cfg.only_media and not comment.media:
            continue

        if not comments_cfg.include_text_comments and not comment.media:
            continue

        if comments_cfg.skip_bot_comments:
            try:
                sender = await comment.get_sender()
                if sender and getattr(sender, 'bot', False):
                    continue
            except Exception:
                pass

        filtered.append(comment)

    if not filtered:
        logging.info(
            f"💬 帖子 {src_post_id}: {len(comments)} 条评论全被过滤"
        )
        return

    # ========== 分组 ==========
    send_units = _group_comments(filtered)
    single_count = sum(1 for u in send_units if len(u) == 1)
    group_count = sum(1 for u in send_units if len(u) > 1)
    logging.info(
        f"💬 帖子 {src_post_id}: {len(filtered)} 条 → "
        f"{len(send_units)} 单元 ({single_count} 单条 + {group_count} 组)"
    )

    # ========== 确定目标 ==========
    dest_targets = {}  # { chat_id: reply_to_id }

    for dest_ch in dest_list:
        dest_resolved = dest_ch
        if not isinstance(dest_resolved, int):
            try:
                dest_resolved = await config.get_id(client, dest_ch)
            except Exception:
                continue

        dest_post_id = st.get_dest_post_id(
            src_channel_id, src_post_id, dest_resolved
        )
        if dest_post_id is None:
            logging.warning(
                f"⚠️ 帖子 {src_post_id} → 目标 {dest_resolved}: "
                f"没有帖子映射（主帖子可能转发失败）"
            )
            continue

        if comments_cfg.dest_mode == "comments":
            try:
                dest_disc = await get_discussion_message(
                    client, dest_resolved, dest_post_id
                )
                if dest_disc:
                    dest_targets[dest_disc.chat_id] = dest_disc.id
                    logging.info(
                        f"💬 目标: dest_ch={dest_resolved}, "
                        f"disc_chat={dest_disc.chat_id}, "
                        f"disc_msg={dest_disc.id}"
                    )
                else:
                    # 回退：直接回复到频道帖子
                    dest_targets[dest_resolved] = dest_post_id
                    logging.info(
                        f"💬 目标(回退): 直接回复 {dest_resolved}/{dest_post_id}"
                    )
            except Exception as e:
                logging.warning(f"⚠️ 获取目标讨论消息失败: {e}")
                dest_targets[dest_resolved] = dest_post_id
        elif comments_cfg.dest_mode == "discussion":
            for dg in comments_cfg.dest_discussion_groups:
                dg_id = dg
                if not isinstance(dg_id, int):
                    try:
                        dg_id = await config.get_id(client, dg)
                    except Exception:
                        continue
                dest_targets[dg_id] = None

    if not dest_targets:
        logging.warning(f"⚠️ 帖子 {src_post_id} 没有有效的评论目标")
        return

    # ========== 逐单元发送 ==========
    sent_count = 0
    fail_count = 0

    for unit_idx, unit_msgs in enumerate(send_units):
        is_group = len(unit_msgs) > 1

        if is_group:
            gid = unit_msgs[0].grouped_id
            logging.info(
                f"💬 [{unit_idx+1}/{len(send_units)}] "
                f"媒体组 gid={gid} ({len(unit_msgs)} 条)"
            )

            tms = await apply_plugins_to_group(unit_msgs)
            if not tms:
                continue

            tm_template = tms[0]
            if tm_template is None:
                continue

            for dest_chat_id, dest_reply_to in dest_targets.items():
                try:
                    fwded = await send_message(
                        dest_chat_id, tm_template,
                        grouped_messages=[tm.message for tm in tms],
                        grouped_tms=tms,
                        comment_to_post=dest_reply_to,
                    )
                    if fwded:
                        sent_count += 1
                        fwded_id = _extract_msg_id(fwded)
                        if fwded_id:
                            st.add_comment_mapping(
                                src_channel_id, unit_msgs[0].id,
                                dest_chat_id, fwded_id,
                            )
                        logging.info(f"✅ 评论媒体组 → {dest_chat_id}")
                    else:
                        fail_count += 1
                except FloodWaitError as fwe:
                    logging.warning(f"⛔ FloodWait: {fwe.seconds}s")
                    await asyncio.sleep(fwe.seconds)
                    try:
                        fwded = await send_message(
                            dest_chat_id, tm_template,
                            grouped_messages=[tm.message for tm in tms],
                            grouped_tms=tms,
                            comment_to_post=dest_reply_to,
                        )
                        if fwded:
                            sent_count += 1
                    except Exception:
                        fail_count += 1
                except Exception as e:
                    fail_count += 1
                    logging.error(f"❌ 评论媒体组失败: {e}")

            for tm in tms:
                tm.clear()

        else:
            comment = unit_msgs[0]
            media_info = ""
            if comment.photo:
                media_info = "📷"
            elif comment.video:
                media_info = "🎬"
            elif comment.document:
                media_info = "📄"

            logging.info(
                f"💬 [{unit_idx+1}/{len(send_units)}] "
                f"单条 #{comment.id} {media_info}"
            )

            tm = await apply_plugins(comment)
            if not tm:
                continue

            for dest_chat_id, dest_reply_to in dest_targets.items():
                try:
                    fwded = await send_message(
                        dest_chat_id, tm,
                        comment_to_post=dest_reply_to,
                    )
                    if fwded:
                        sent_count += 1
                        fwded_id = _extract_msg_id(fwded)
                        if fwded_id:
                            st.add_comment_mapping(
                                src_channel_id, comment.id,
                                dest_chat_id, fwded_id,
                            )
                        logging.info(f"✅ 评论 #{comment.id} → {dest_chat_id}")
                    else:
                        fail_count += 1
                except FloodWaitError as fwe:
                    logging.warning(f"⛔ FloodWait: {fwe.seconds}s")
                    await asyncio.sleep(fwe.seconds)
                    try:
                        fwded = await send_message(
                            dest_chat_id, tm,
                            comment_to_post=dest_reply_to,
                        )
                        if fwded:
                            sent_count += 1
                    except Exception:
                        fail_count += 1
                except Exception as e:
                    fail_count += 1
                    logging.error(f"❌ 评论 #{comment.id} 失败: {e}")

            tm.clear()

        # 单元间延迟
        delay = random.randint(5, 20)
        await asyncio.sleep(delay)

    logging.info(
        f"💬 ═══ 帖子 {src_post_id} 评论完成: "
        f"成功={sent_count} 失败={fail_count} ═══"
    )


# =====================================================================
#  主 forward_job
# =====================================================================


async def forward_job() -> None:
    clean_session_files()
    await load_async_plugins()

    if CONFIG.login.user_type != 1:
        logging.error("❌ past 模式仅支持用户账号")
        return

    if not CONFIG.login.SESSION_STRING:
        logging.error("❌ Session String 为空")
        return

    SESSION = get_SESSION()

    async with TelegramClient(
        SESSION, CONFIG.login.API_ID, CONFIG.login.API_HASH
    ) as client:

        is_bot = await client.is_bot()
        me = await client.get_me()

        if is_bot:
            logging.error(
                "❌ Bot 账号 (%s @%s) 无法用 past 模式",
                me.first_name or "Bot", me.username or "N/A",
            )
            return

        logging.info(
            "✅ 用户: %s %s (@%s, id=%d)",
            me.first_name or "", me.last_name or "",
            me.username or "N/A", me.id,
        )

        config.from_to = await config.load_from_to(client, CONFIG.forwards)

        if not config.from_to:
            logging.error("❌ 没有有效的转发连接")
            return

        for from_to, forward in zip(config.from_to.items(), CONFIG.forwards):
            src, dest = from_to
            last_id = 0
            grouped_buffer: Dict[int, List[Message]] = defaultdict(list)

            logging.info(
                "📡 转发: %d → %s (offset=%d, end=%s, comments=%s)",
                src, dest, forward.offset, forward.end,
                "ON" if forward.comments.enabled else "OFF",
            )

            async for message in client.iter_messages(
                src, reverse=True, offset_id=forward.offset
            ):
                if isinstance(message, MessageService):
                    continue

                if forward.end and message.id > forward.end:
                    logging.info(f"📍 end={forward.end}, 停止")
                    break

                try:
                    current_gid = message.grouped_id

                    # 刷新之前的媒体组
                    if grouped_buffer and (
                        current_gid is None
                        or (current_gid is not None
                            and current_gid not in grouped_buffer)
                    ):
                        try:
                            flushed = await _flush_grouped_buffer(
                                client, src, dest, grouped_buffer, forward
                            )
                            if flushed:
                                last_id = max(last_id, flushed)
                        except FloodWaitError as fwe:
                            await asyncio.sleep(fwe.seconds)
                            flushed = await _flush_grouped_buffer(
                                client, src, dest, grouped_buffer, forward
                            )
                            if flushed:
                                last_id = max(last_id, flushed)

                    if current_gid is not None:
                        grouped_buffer[current_gid].append(message)
                        continue

                    # 单条消息
                    tm = await apply_plugins(message)
                    if not tm:
                        continue

                    event_uid = st.EventUid(
                        st.DummyEvent(message.chat_id, message.id)
                    )
                    st.stored[event_uid] = {}

                    for d in dest:
                        reply_to_id = None
                        if message.is_reply:
                            rmid = _get_reply_to_msg_id(message)
                            if rmid is not None:
                                r_uid = st.EventUid(
                                    st.DummyEvent(message.chat_id, rmid)
                                )
                                if r_uid in st.stored:
                                    fr = st.stored[r_uid].get(d)
                                    if fr is not None:
                                        reply_to_id = (
                                            fr if isinstance(fr, int)
                                            else getattr(fr, 'id', None)
                                        )
                        tm.reply_to = reply_to_id

                        try:
                            fwded_msg = await send_message(d, tm)
                            if fwded_msg is not None:
                                st.stored[event_uid][d] = fwded_msg
                                fwded_id = _extract_msg_id(fwded_msg)
                                if fwded_id is not None:
                                    st.add_post_mapping(
                                        src, message.id, d, fwded_id
                                    )
                        except Exception as e:
                            logging.error(f"❌ 发送失败: {e}")

                    tm.clear()
                    last_id = message.id
                    forward.offset = last_id
                    write_config(CONFIG, persist=False)

                    # ★★★ 转发评论区 ★★★
                    if forward.comments.enabled:
                        logging.info(
                            f"💬 准备转发帖子 {message.id} 的评论..."
                        )
                        try:
                            await _forward_comments_for_post(
                                client, src, message.id, dest, forward
                            )
                        except Exception as e:
                            logging.error(
                                f"❌ 帖子 {message.id} 评论失败: {e}",
                                exc_info=True,
                            )

                    delay = random.randint(60, 300)
                    logging.info(f"⏸️ 休息 {delay}s (msg {message.id})")
                    await asyncio.sleep(delay)

                except FloodWaitError as fwe:
                    logging.warning(f"⛔ FloodWait: {fwe.seconds}s")
                    await asyncio.sleep(fwe.seconds)
                except Exception as err:
                    logging.exception(err)

            if grouped_buffer:
                try:
                    await _flush_grouped_buffer(
                        client, src, dest, grouped_buffer, forward
                    )
                except Exception as e:
                    logging.exception(f"🚨 刷新剩余组失败: {e}")

        logging.info("🏁 past 模式完成")
