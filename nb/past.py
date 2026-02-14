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
    _download_media_robust,
    _copy_album,
    _get_download_client,
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
#  评论区 past 模式（完全重写）
# =====================================================================


async def _forward_comments_for_post(
    client: TelegramClient,
    src_channel_id: int,
    src_post_id: int,
    forward: config.Forward,
) -> None:
    """遍历某个帖子的所有评论并转发到目标帖子的评论区。
    
    支持：
    - 单条文本/媒体评论
    - 媒体组评论（grouped_id 相同的评论成组发送）
    - 正确 reply_to 目标帖子（评论出现在评论区）
    """
    comments_cfg = forward.comments

    # 获取源讨论组中该帖子的讨论消息
    src_disc_msg = await get_discussion_message(client, src_channel_id, src_post_id)
    if src_disc_msg is None:
        logging.debug(f"帖子 {src_post_id} 没有讨论消息，跳过评论")
        return

    src_discussion_id = src_disc_msg.chat_id
    src_top_id = src_disc_msg.id

    # 记录讨论组帖子副本映射
    st.discussion_to_channel_post[(src_discussion_id, src_top_id)] = src_post_id

    # 确定目标: { dest_discussion_id: dest_top_id }
    dest_targets = {}

    for dest_channel_id in forward.dest:
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
            logging.debug(
                f"帖子 {src_post_id} 在目标 {dest_resolved} 没有映射，跳过评论"
            )
            continue

        if comments_cfg.dest_mode == "comments":
            dest_disc_msg = await get_discussion_message(
                client, dest_resolved, dest_post_id
            )
            if dest_disc_msg:
                dest_targets[dest_disc_msg.chat_id] = dest_disc_msg.id
                logging.info(
                    f"💬 评论目标: discussion={dest_disc_msg.chat_id}, "
                    f"reply_to={dest_disc_msg.id}"
                )
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
        logging.debug(f"帖子 {src_post_id} 没有有效的评论目标")
        return

    # =====================================================================
    #  收集评论，处理媒体组
    # =====================================================================

    comment_count = 0
    grouped_buffer: Dict[int, List[Message]] = defaultdict(list)
    # grouped_id → [messages]

    async for comment in client.iter_messages(
        src_discussion_id,
        reply_to=src_top_id,
        reverse=True,
    ):
        if isinstance(comment, MessageService):
            continue

        # 跳过频道帖子副本
        if hasattr(comment, 'fwd_from') and comment.fwd_from:
            if getattr(comment.fwd_from, 'channel_post', None):
                continue

        # 过滤
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

        # 媒体组：先缓存
        if comment.grouped_id is not None:
            # 如果之前缓存了不同的 grouped_id，先发送之前的
            other_groups = [
                gid for gid in grouped_buffer
                if gid != comment.grouped_id
            ]
            for old_gid in other_groups:
                await _send_comment_group(
                    client, grouped_buffer[old_gid],
                    dest_targets, comments_cfg,
                )
                comment_count += len(grouped_buffer[old_gid])
                del grouped_buffer[old_gid]

                delay = random.randint(10, 60)
                await asyncio.sleep(delay)

            grouped_buffer[comment.grouped_id].append(comment)
            continue

        # 如果有未发送的媒体组，先发送
        for old_gid in list(grouped_buffer.keys()):
            await _send_comment_group(
                client, grouped_buffer[old_gid],
                dest_targets, comments_cfg,
            )
            comment_count += len(grouped_buffer[old_gid])
            del grouped_buffer[old_gid]

            delay = random.randint(10, 60)
            await asyncio.sleep(delay)

        # 单条评论
        await _send_single_comment(
            client, comment, dest_targets, comments_cfg,
        )
        comment_count += 1

        delay = random.randint(10, 60)
        await asyncio.sleep(delay)

    # 发送剩余的媒体组
    for old_gid in list(grouped_buffer.keys()):
        await _send_comment_group(
            client, grouped_buffer[old_gid],
            dest_targets, comments_cfg,
        )
        comment_count += len(grouped_buffer[old_gid])

    if comment_count > 0:
        logging.info(
            f"💬 帖子 {src_post_id} 的评论转发完成: {comment_count} 条评论"
        )


async def _send_single_comment(
    client: TelegramClient,
    comment: Message,
    dest_targets: Dict[int, Optional[int]],
    comments_cfg,
) -> None:
    """发送单条评论到所有目标讨论组的对应帖子评论区。"""
    tm = await apply_plugins(comment)
    if not tm:
        return

    download_client = _get_download_client(tm)

    for dest_disc_id, dest_top_id in dest_targets.items():
        try:
            fwded = await send_message(
                dest_disc_id,
                tm,
                comment_to_post=dest_top_id,
            )
            if fwded:
                st.add_comment_mapping(
                    comment.chat_id, comment.id,
                    dest_disc_id, _extract_msg_id(fwded),
                )
                logging.info(
                    f"💬 评论转发成功: {comment.chat_id}/{comment.id} → "
                    f"{dest_disc_id} (reply_to={dest_top_id})"
                )
            else:
                logging.warning(
                    f"⚠️ 评论转发返回 None: {comment.id} → {dest_disc_id}"
                )
        except FloodWaitError as fwe:
            logging.warning(f"⛔ FloodWait (评论): 等待 {fwe.seconds} 秒")
            await asyncio.sleep(fwe.seconds + 10)
            try:
                fwded = await send_message(
                    dest_disc_id, tm, comment_to_post=dest_top_id,
                )
                if fwded:
                    logging.info(f"💬 评论重试成功: {comment.id}")
            except Exception as e2:
                logging.error(f"❌ 评论重试失败: {e2}")
        except Exception as e:
            logging.error(f"❌ 评论发送失败: {e}")

    tm.clear()


async def _send_comment_group(
    client: TelegramClient,
    comments: List[Message],
    dest_targets: Dict[int, Optional[int]],
    comments_cfg,
) -> None:
    """发送一组评论（媒体组）到所有目标讨论组的对应帖子评论区。"""
    if not comments:
        return

    tms = await apply_plugins_to_group(comments)
    if not tms:
        return

    tm_template = tms[0]
    download_client = _get_download_client(tm_template)
    send_client = tm_template.client

    for dest_disc_id, dest_top_id in dest_targets.items():
        try:
            fwded = await send_message(
                dest_disc_id,
                tm_template,
                grouped_messages=[tm.message for tm in tms],
                grouped_tms=tms,
                comment_to_post=dest_top_id,
            )
            if fwded:
                fwded_id = _extract_msg_id(fwded)
                st.add_comment_mapping(
                    comments[0].chat_id, comments[0].id,
                    dest_disc_id, fwded_id,
                )
                logging.info(
                    f"💬 评论媒体组转发成功: {len(comments)} 条 → "
                    f"{dest_disc_id} (reply_to={dest_top_id})"
                )
            else:
                logging.warning(
                    f"⚠️ 评论媒体组转发返回 None → {dest_disc_id}"
                )
        except FloodWaitError as fwe:
            logging.warning(f"⛔ FloodWait (评论组): 等待 {fwe.seconds} 秒")
            await asyncio.sleep(fwe.seconds + 10)
            try:
                fwded = await send_message(
                    dest_disc_id, tm_template,
                    grouped_messages=[tm.message for tm in tms],
                    grouped_tms=tms,
                    comment_to_post=dest_top_id,
                )
                if fwded:
                    logging.info(f"💬 评论媒体组重试成功")
            except Exception as e2:
                logging.error(f"❌ 评论媒体组重试失败: {e2}")
        except Exception as e:
            logging.error(f"❌ 评论媒体组发送失败: {e}")

    for tm in tms:
        tm.clear()


# =====================================================================
#  主 forward_job
# =====================================================================


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
            prev_grouped_id: Optional[int] = None

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
                            logging.warning(f"⛔ FloodWait (组刷新): 等待 {fwe.seconds} 秒")
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

                    # ★ 转发该帖子的评论区
                    if forward.comments.enabled:
                        try:
                            await _forward_comments_for_post(
                                client, src, message.id, forward
                            )
                        except Exception as e:
                            logging.error(
                                f"❌ 帖子 {message.id} 评论转发失败: {e}"
                            )

                    delay_seconds = random.randint(60, 300)
                    logging.info(f"⏸️ 休息 {delay_seconds} 秒 (单条消息 {message.id})")
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
