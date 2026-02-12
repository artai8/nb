# nb/past.py

import asyncio
import logging
import random
from collections import defaultdict, OrderedDict
from typing import List, Dict, Optional, Tuple

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
    get_comments_for_post,
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


async def _send_past_grouped(
    client: TelegramClient, src: int, dest: List[int], messages: List[Message]
) -> bool:
    tms = await apply_plugins_to_group(messages)
    if not tms:
        logging.warning("⚠️ 所有消息被插件过滤，跳过该媒体组")
        return False

    tm_template = tms[0]
    if tm_template is None:
        logging.warning("⚠️ 模板消息为 None，跳过该媒体组")
        return False

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

            fwded_id = _extract_msg_id(fwded_msgs)
            if fwded_id is not None:
                st.add_post_mapping(src, first_msg_id, d, fwded_id)

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
    last_id = 0
    for gid, msgs in list(grouped_buffer.items()):
        if not msgs:
            continue

        await _send_past_grouped(client, src, dest, msgs)

        group_last_id = max(m.id for m in msgs)
        last_id = max(last_id, group_last_id)

        forward.offset = group_last_id
        write_config(CONFIG, persist=False)

        logging.info(f"✅ 媒体组 {gid} ({len(msgs)} 条) 发送完成, offset → {group_last_id}")

        delay_seconds = random.randint(60, 300)
        logging.info(f"⏸️ 媒体组发送后休息 {delay_seconds} 秒")
        await asyncio.sleep(delay_seconds)

    grouped_buffer.clear()
    return last_id


# =====================================================================
#  评论区：将评论列表整理为有序的发送单元
#  每个单元是 (单条消息) 或 (媒体组消息列表)
# =====================================================================


def _group_comments(
    comments: List[Message],
) -> List[List[Message]]:
    """将评论列表按 grouped_id 整理为发送单元。

    返回一个列表，每个元素是:
    - [single_msg]           — 单条消息（无 grouped_id 或独立消息）
    - [msg1, msg2, msg3...]  — 同一 grouped_id 的媒体组

    顺序保持与原始评论顺序一致（按第一条消息出现的顺序）。
    """
    units: List[List[Message]] = []
    # grouped_id → 在 units 中的索引
    group_index: Dict[int, int] = {}

    for msg in comments:
        gid = getattr(msg, 'grouped_id', None)

        if gid is None:
            # 单条消息
            units.append([msg])
        else:
            if gid in group_index:
                # 已有这个组，追加
                units[group_index[gid]].append(msg)
            else:
                # 新组
                group_index[gid] = len(units)
                units.append([msg])

    return units


# =====================================================================
#  评论区 past 模式
# =====================================================================


async def _forward_comments_for_post(
    client: TelegramClient,
    src_channel_id: int,
    src_post_id: int,
    dest_list: List[int],
    forward: config.Forward,
) -> None:
    """获取源频道帖子的评论并转发到目标频道帖子的评论区。

    ★ 支持媒体组：同一 grouped_id 的评论作为一个组发送。
    """
    comments_cfg = forward.comments

    logging.info(
        f"💬 开始获取帖子 {src_post_id} 的评论 "
        f"(channel={src_channel_id})"
    )

    # ========== 获取评论 ==========
    comments = []

    # 方法 A: 直接从频道获取
    try:
        comments = await get_comments_for_post(
            client, src_channel_id, src_post_id
        )
    except Exception as e:
        logging.warning(f"⚠️ 方法A获取评论失败: {e}")

    # 方法 B: 通过讨论组获取
    if not comments:
        logging.info(f"💬 方法A未获取到评论，尝试方法B（通过讨论组）")
        try:
            src_disc_msg = await get_discussion_message(
                client, src_channel_id, src_post_id
            )
            if src_disc_msg:
                src_discussion_id = src_disc_msg.chat_id
                src_top_id = src_disc_msg.id

                st.discussion_to_channel_post[
                    (src_discussion_id, src_top_id)
                ] = src_post_id

                async for msg in client.iter_messages(
                    src_discussion_id,
                    reply_to=src_top_id,
                    reverse=True,
                ):
                    comments.append(msg)

                logging.info(f"💬 方法B获取到 {len(comments)} 条评论")
        except Exception as e:
            logging.warning(f"⚠️ 方法B获取评论也失败: {e}")

    if not comments:
        logging.info(f"💬 帖子 {src_post_id} 没有评论，跳过")
        return

    # ========== 预过滤 ==========
    filtered_comments = []
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

        filtered_comments.append(comment)

    if not filtered_comments:
        logging.info(
            f"💬 帖子 {src_post_id}: {len(comments)} 条评论全部被过滤"
        )
        return

    # ========== 整理为发送单元（单条 / 媒体组）==========
    send_units = _group_comments(filtered_comments)

    single_count = sum(1 for u in send_units if len(u) == 1)
    group_count = sum(1 for u in send_units if len(u) > 1)
    logging.info(
        f"💬 帖子 {src_post_id}: "
        f"{len(filtered_comments)} 条评论 → "
        f"{len(send_units)} 个发送单元 "
        f"({single_count} 单条 + {group_count} 媒体组)"
    )

    # ========== 确定目标 ==========
    dest_targets = {}  # { dest_chat_id: dest_reply_to_id }

    for dest_channel_id in dest_list:
        dest_resolved = dest_channel_id
        if not isinstance(dest_resolved, int):
            try:
                dest_resolved = await config.get_id(client, dest_channel_id)
            except Exception:
                continue

        dest_post_id = st.get_dest_post_id(
            src_channel_id, src_post_id, dest_resolved
        )
        if dest_post_id is None:
            logging.warning(
                f"⚠️ 帖子 {src_post_id} 在目标 {dest_resolved} 没有映射"
            )
            continue

        if comments_cfg.dest_mode == "comments":
            try:
                dest_disc_msg = await get_discussion_message(
                    client, dest_resolved, dest_post_id
                )
                if dest_disc_msg:
                    dest_targets[dest_disc_msg.chat_id] = dest_disc_msg.id
                else:
                    dest_targets[dest_resolved] = dest_post_id
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

    for unit_idx, unit_messages in enumerate(send_units):
        is_group = len(unit_messages) > 1

        if is_group:
            # ★★★ 媒体组：整组发送 ★★★
            gid = unit_messages[0].grouped_id
            logging.info(
                f"💬 发送媒体组 (grouped_id={gid}, "
                f"{len(unit_messages)} 条) [{unit_idx+1}/{len(send_units)}]"
            )

            tms = await apply_plugins_to_group(unit_messages)
            if not tms:
                logging.info(f"💬 媒体组 {gid} 被插件过滤")
                continue

            tm_template = tms[0]
            if tm_template is None:
                continue

            for dest_chat_id, dest_reply_to in dest_targets.items():
                try:
                    fwded = await send_message(
                        dest_chat_id,
                        tm_template,
                        grouped_messages=[tm.message for tm in tms],
                        grouped_tms=tms,
                        comment_to_post=dest_reply_to,
                    )
                    if fwded:
                        sent_count += 1
                        fwded_id = _extract_msg_id(fwded)
                        if fwded_id:
                            st.add_comment_mapping(
                                src_channel_id, unit_messages[0].id,
                                dest_chat_id, fwded_id,
                            )
                        logging.info(
                            f"✅ 评论媒体组转发成功 → chat={dest_chat_id}"
                        )
                    else:
                        fail_count += 1
                        logging.warning(f"⚠️ 评论媒体组转发返回 None")
                except FloodWaitError as fwe:
                    logging.warning(f"⛔ FloodWait: 等待 {fwe.seconds} 秒")
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
                    except Exception as e2:
                        fail_count += 1
                        logging.error(f"❌ 媒体组重试失败: {e2}")
                except Exception as e:
                    fail_count += 1
                    logging.error(f"❌ 评论媒体组转发失败: {e}")

            # 清理
            for tm in tms:
                tm.clear()

        else:
            # ★★★ 单条消息 ★★★
            comment = unit_messages[0]

            media_type = "无媒体"
            if comment.photo:
                media_type = "📷"
            elif comment.video:
                media_type = "🎬"
            elif comment.gif:
                media_type = "🎞️"
            elif comment.document:
                media_type = "📄"

            text_preview = (comment.text or "")[:30]
            logging.info(
                f"💬 发送单条评论 #{comment.id} "
                f"{media_type} '{text_preview}' "
                f"[{unit_idx+1}/{len(send_units)}]"
            )

            tm = await apply_plugins(comment)
            if not tm:
                logging.info(f"💬 评论 #{comment.id} 被插件过滤")
                continue

            for dest_chat_id, dest_reply_to in dest_targets.items():
                try:
                    fwded = await send_message(
                        dest_chat_id,
                        tm,
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
                        logging.info(
                            f"✅ 评论转发成功 #{comment.id} → "
                            f"chat={dest_chat_id}"
                        )
                    else:
                        fail_count += 1
                except FloodWaitError as fwe:
                    logging.warning(f"⛔ FloodWait: 等待 {fwe.seconds} 秒")
                    await asyncio.sleep(fwe.seconds)
                    try:
                        fwded = await send_message(
                            dest_chat_id, tm,
                            comment_to_post=dest_reply_to,
                        )
                        if fwded:
                            sent_count += 1
                    except Exception as e2:
                        fail_count += 1
                        logging.error(f"❌ 重试失败: {e2}")
                except Exception as e:
                    fail_count += 1
                    logging.error(f"❌ 评论转发失败 #{comment.id}: {e}")

            tm.clear()

        # 每个发送单元之间的延迟
        delay = random.randint(5, 30)
        await asyncio.sleep(delay)

    logging.info(
        f"💬 帖子 {src_post_id} 评论转发完成: "
        f"成功 {sent_count}, 失败 {fail_count}, "
        f"共 {len(send_units)} 个发送单元"
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

    async with TelegramClient(SESSION, CONFIG.login.API_ID, CONFIG.login.API_HASH) as client:

        is_bot = await client.is_bot()
        me = await client.get_me()

        if is_bot:
            logging.error(
                "❌ 当前是 Bot 账号 (%s @%s)，无法使用 past 模式！",
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
            prev_grouped_id: Optional[int] = None

            logging.info(
                "📡 开始转发: %d → %s (offset=%d, end=%s, comments=%s)",
                src, dest, forward.offset, forward.end,
                "启用" if forward.comments.enabled else "关闭",
            )

            async for message in client.iter_messages(src, reverse=True, offset_id=forward.offset):
                if isinstance(message, MessageService):
                    continue

                if forward.end and message.id > forward.end:
                    logging.info(f"📍 到达 end={forward.end}, 停止")
                    break

                try:
                    current_grouped_id = message.grouped_id

                    if grouped_buffer and (
                        current_grouped_id is None
                        or (current_grouped_id is not None
                            and current_grouped_id not in grouped_buffer)
                    ):
                        try:
                            flushed_last = await _flush_grouped_buffer(
                                client, src, dest, grouped_buffer, forward
                            )
                            if flushed_last:
                                last_id = max(last_id, flushed_last)
                        except FloodWaitError as fwe:
                            logging.warning(f"⛔ FloodWait: 等待 {fwe.seconds} 秒")
                            await asyncio.sleep(fwe.seconds)
                            flushed_last = await _flush_grouped_buffer(
                                client, src, dest, grouped_buffer, forward
                            )
                            if flushed_last:
                                last_id = max(last_id, flushed_last)

                    if current_grouped_id is not None:
                        grouped_buffer[current_grouped_id].append(message)
                        prev_grouped_id = current_grouped_id
                        continue

                    prev_grouped_id = None

                    tm = await apply_plugins(message)
                    if not tm:
                        continue

                    event_uid = st.EventUid(st.DummyEvent(message.chat_id, message.id))
                    st.stored[event_uid] = {}

                    for d in dest:
                        reply_to_id = None
                        if message.is_reply:
                            reply_msg_id = _get_reply_to_msg_id(message)
                            if reply_msg_id is not None:
                                r_event = st.DummyEvent(message.chat_id, reply_msg_id)
                                r_event_uid = st.EventUid(r_event)
                                if r_event_uid in st.stored:
                                    fwded_reply = st.stored[r_event_uid].get(d)
                                    if fwded_reply is not None:
                                        if isinstance(fwded_reply, int):
                                            reply_to_id = fwded_reply
                                        elif hasattr(fwded_reply, 'id'):
                                            reply_to_id = fwded_reply.id
                        tm.reply_to = reply_to_id

                        try:
                            fwded_msg = await send_message(d, tm)
                            if fwded_msg is not None:
                                st.stored[event_uid][d] = fwded_msg

                                fwded_id = _extract_msg_id(fwded_msg)
                                if fwded_id is not None:
                                    st.add_post_mapping(src, message.id, d, fwded_id)
                            else:
                                logging.warning(f"⚠️ 发送返回 None, dest={d}, msg={message.id}")
                        except Exception as e:
                            logging.error(f"❌ 单条发送失败: {e}")

                    tm.clear()
                    last_id = message.id
                    forward.offset = last_id
                    write_config(CONFIG, persist=False)

                    # ★★★ 转发该帖子的评论区 ★★★
                    if forward.comments.enabled:
                        logging.info(f"💬 准备转发帖子 {message.id} 的评论...")
                        try:
                            await _forward_comments_for_post(
                                client, src, message.id, dest, forward
                            )
                        except Exception as e:
                            logging.error(
                                f"❌ 帖子 {message.id} 评论转发失败: {e}",
                                exc_info=True,
                            )

                    delay_seconds = random.randint(60, 300)
                    logging.info(f"⏸️ 休息 {delay_seconds} 秒 (消息 {message.id})")
                    await asyncio.sleep(delay_seconds)

                except FloodWaitError as fwe:
                    logging.warning(f"⛔ FloodWait: 等待 {fwe.seconds} 秒")
                    await asyncio.sleep(fwe.seconds)
                except Exception as err:
                    logging.exception(err)

            if grouped_buffer:
                logging.info(f"📦 刷新剩余 {len(grouped_buffer)} 个媒体组")
                try:
                    await _flush_grouped_buffer(client, src, dest, grouped_buffer, forward)
                except Exception as e:
                    logging.exception(f"🚨 刷新剩余组失败: {e}")

        logging.info("🏁 past 模式转发完成")
