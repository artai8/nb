# nb/past.py — 导入已清理，不引用不存在的函数

import asyncio
import logging
import random
from collections import defaultdict
from typing import List, Dict, Optional

from telethon import TelegramClient
from telethon.errors.rpcerrorlist import FloodWaitError, MsgIdInvalidError
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
    _auto_comment_keyword,
    _extract_comment_keyword,
    resolve_bot_media_from_message,
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


async def _collect_bot_media_from_comments(
    client: TelegramClient,
    src_channel_id: int,
    src_post_id: int,
    forward,
) -> List[Message]:
    if not _bot_media_allowed(forward):
        logging.info(f"🤖 bot_media 未启用, 跳过 post={src_post_id}")
        return []

    logging.info(f"🤖 开始获取讨论消息 channel={src_channel_id} post={src_post_id}")

    try:
        disc_msg = await get_discussion_message(client, src_channel_id, src_post_id)
    except Exception as e:
        logging.warning(f"⚠️ 获取讨论消息异常 post={src_post_id}: {e}")
        return []

    if disc_msg is None:
        logging.info(f"🤖 帖子 {src_post_id} 无讨论消息, 跳过")
        return []

    src_discussion_id = disc_msg.chat_id
    src_top_id = disc_msg.id
    logging.info(f"🤖 讨论组={src_discussion_id} top_id={src_top_id}")

    comment_count = 0
    collected: List[Message] = []

    try:
        async for comment in client.iter_messages(
            src_discussion_id, reply_to=src_top_id, reverse=True,
        ):
            if isinstance(comment, MessageService):
                logging.debug(f"🤖 跳过 MessageService #{comment.id}")
                continue

            comment_count += 1
            text_preview = (comment.raw_text or comment.text or "")[:150]
            has_markup = comment.reply_markup is not None
            sender_id = comment.sender_id
            fwd = comment.fwd_from
            logging.info(
                f"🤖 评论#{comment.id} sender={sender_id} fwd={fwd is not None} "
                f"markup={has_markup} text={text_preview!r}"
            )

            try:
                bot_media = await resolve_bot_media_from_message(client, comment, forward)
            except Exception as e:
                logging.warning(f"⚠️ 评论#{comment.id} bot媒体解析异常: {e}")
                bot_media = []

            if bot_media:
                logging.info(f"🤖 ✅ 评论#{comment.id} 命中 {len(bot_media)} 条bot媒体")
                collected.extend(bot_media)
            else:
                logging.debug(f"🤖 评论#{comment.id} 无bot媒体")
    except MsgIdInvalidError as e:
        logging.warning(f"⚠️ 讨论区消息 ID 无效, 跳过评论拉取 post={src_post_id}: {e}")
        return []

    logging.info(
        f"🤖 评论区扫描完成 post={src_post_id}: "
        f"{comment_count} 条评论, 收集 {len(collected)} 条媒体"
    )
    return _dedupe_messages(collected) if collected else []


async def _send_combined_album(
    src: int,
    dest: List[int],
    first_msg_id: int,
    combined_messages: List[Message],
) -> bool:
    tms = await apply_plugins_to_group(combined_messages)
    if not tms:
        logging.warning("⚠️ 合并媒体组全部被插件过滤，跳过")
        return False
    tm_template = tms[0]
    if tm_template is None:
        logging.warning("⚠️ 合并媒体组模板消息为 None，跳过")
        return False
    for d in dest:
        try:
            fwded_msgs = await send_message(
                d,
                tm_template,
                grouped_messages=[tm.message for tm in tms],
                grouped_tms=tms,
            )
            event_uid = st.EventUid(st.DummyEvent(src, first_msg_id))
            st.stored[event_uid] = {d: fwded_msgs}
            fwded_id = _extract_msg_id(fwded_msgs)
            if fwded_id is not None:
                st.add_post_mapping(src, first_msg_id, d, fwded_id)
        except Exception as e:
            logging.critical(f"🚨 合并媒体组播失败: {e}")
    for tm in tms:
        tm.clear()
    return True


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


async def _send_past_grouped(
    client: TelegramClient, src: int, dest: List[int], messages: List[Message], forward
) -> bool:
    comment_bot_media = await _collect_bot_media_from_comments(client, src, messages[0].id, forward)
    if comment_bot_media:
        combined_messages = messages + comment_bot_media
        return await _send_combined_album(src, dest, messages[0].id, combined_messages)
    bot_media = []
    bot_media_allowed = _bot_media_allowed(forward)
    auto_comment_allowed = (forward is None or forward.auto_comment_trigger_enabled is not False)
    if bot_media_allowed and auto_comment_allowed:
        for msg in messages:
            keyword = _extract_comment_keyword(msg.raw_text or msg.text or "", forward)
            if keyword:
                await _auto_comment_keyword(client, src, msg.id, keyword)
                break
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
                fwded_msgs = await _send_bot_media_album(
                    d,
                    bot_media,
                    base_text=trigger_text,
                )
                first_msg_id = messages[0].id
                event_uid = st.EventUid(st.DummyEvent(src, first_msg_id))
                st.stored[event_uid] = {d: fwded_msgs}
                fwded_id = _extract_msg_id(fwded_msgs)
                if fwded_id is not None:
                    st.add_post_mapping(src, first_msg_id, d, fwded_id)
            except Exception as e:
                logging.critical(f"🚨 bot 媒体组播失败: {e}")
        return True
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

        await _send_past_grouped(client, src, dest, msgs, forward)

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
#  评论区 past 模式
# =====================================================================


async def _forward_comments_for_post(
    client: TelegramClient,
    src_channel_id: int,
    src_post_id: int,
    forward: config.Forward,
) -> None:
    comments_cfg = forward.comments

    src_disc_msg = await get_discussion_message(client, src_channel_id, src_post_id)
    if src_disc_msg is None:
        logging.debug(f"帖子 {src_post_id} 没有讨论消息，跳过评论")
        return

    src_discussion_id = src_disc_msg.chat_id
    src_top_id = src_disc_msg.id

    st.discussion_to_channel_post[(src_discussion_id, src_top_id)] = src_post_id

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
            logging.debug(f"帖子 {src_post_id} 在目标 {dest_resolved} 没有映射，跳过评论")
            continue

        if comments_cfg.dest_mode == "comments":
            dest_disc_msg = await get_discussion_message(client, dest_resolved, dest_post_id)
            if dest_disc_msg:
                dest_targets[dest_disc_msg.chat_id] = dest_disc_msg.id
                logging.info(f"💬 评论目标: discussion={dest_disc_msg.chat_id}, reply_to={dest_disc_msg.id}")
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

    comment_count = 0
    grouped_buffer: Dict[int, List[Message]] = defaultdict(list)

    async for comment in client.iter_messages(
        src_discussion_id, reply_to=src_top_id, reverse=True,
    ):
        if isinstance(comment, MessageService):
            continue

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

        if comment.grouped_id is not None:
            other_groups = [gid for gid in grouped_buffer if gid != comment.grouped_id]
            for old_gid in other_groups:
                await _send_comment_group(client, grouped_buffer[old_gid], dest_targets)
                comment_count += len(grouped_buffer[old_gid])
                del grouped_buffer[old_gid]
                delay = random.randint(60, 300)
                await asyncio.sleep(delay)

            grouped_buffer[comment.grouped_id].append(comment)
            continue

        for old_gid in list(grouped_buffer.keys()):
            await _send_comment_group(client, grouped_buffer[old_gid], dest_targets)
            comment_count += len(grouped_buffer[old_gid])
            del grouped_buffer[old_gid]
            delay = random.randint(60, 300)
            await asyncio.sleep(delay)

        bot_media = []
        bot_media_allowed = _bot_media_allowed(forward)
        if bot_media_allowed:
            bot_media = await resolve_bot_media_from_message(client, comment, forward)
        if bot_media:
            bot_media = _dedupe_messages(bot_media)
            for dest_disc_id, dest_top_id in dest_targets.items():
                try:
                    fwded = await _send_bot_media_album(
                        dest_disc_id,
                        bot_media,
                        base_text=comment.raw_text or comment.text or "",
                        comment_to_post=dest_top_id,
                    )
                    if fwded:
                        st.add_comment_mapping(
                            comment.chat_id, comment.id,
                            dest_disc_id, _extract_msg_id(fwded),
                        )
                except Exception as e:
                    logging.error(f"❌ 评论 bot 媒体发送失败: {e}")
        else:
            await _send_single_comment(client, comment, dest_targets)
        comment_count += 1

        delay = random.randint(60, 300)
        await asyncio.sleep(delay)

    for old_gid in list(grouped_buffer.keys()):
        await _send_comment_group(client, grouped_buffer[old_gid], dest_targets)
        comment_count += len(grouped_buffer[old_gid])

    if comment_count > 0:
        logging.info(f"💬 帖子 {src_post_id} 评论转发完成: {comment_count} 条")


async def _send_single_comment(
    client: TelegramClient,
    comment: Message,
    dest_targets: Dict[int, Optional[int]],
) -> None:
    tm = await apply_plugins(comment)
    if not tm:
        return

    for dest_disc_id, dest_top_id in dest_targets.items():
        try:
            fwded = await send_message(dest_disc_id, tm, comment_to_post=dest_top_id)
            if fwded:
                st.add_comment_mapping(
                    comment.chat_id, comment.id,
                    dest_disc_id, _extract_msg_id(fwded),
                )
                logging.info(f"💬 评论转发成功: {comment.chat_id}/{comment.id} → {dest_disc_id}")
            else:
                logging.warning(f"⚠️ 评论转发返回 None: {comment.id}")
        except FloodWaitError as fwe:
            logging.warning(f"⛔ FloodWait (评论): {fwe.seconds} 秒")
            await asyncio.sleep(fwe.seconds + 10)
            try:
                fwded = await send_message(dest_disc_id, tm, comment_to_post=dest_top_id)
                if fwded:
                    logging.info(f"💬 评论重试成功")
            except Exception as e2:
                logging.error(f"❌ 评论重试失败: {e2}")
        except Exception as e:
            logging.error(f"❌ 评论发送失败: {e}")

    tm.clear()


async def _send_comment_group(
    client: TelegramClient,
    comments: List[Message],
    dest_targets: Dict[int, Optional[int]],
) -> None:
    if not comments:
        return

    tms = await apply_plugins_to_group(comments)
    if not tms:
        return

    tm_template = tms[0]

    for dest_disc_id, dest_top_id in dest_targets.items():
        try:
            fwded = await send_message(
                dest_disc_id, tm_template,
                grouped_messages=[tm.message for tm in tms],
                grouped_tms=tms,
                comment_to_post=dest_top_id,
            )
            if fwded:
                st.add_comment_mapping(
                    comments[0].chat_id, comments[0].id,
                    dest_disc_id, _extract_msg_id(fwded),
                )
                logging.info(f"💬 评论媒体组成功: {len(comments)} 条 → {dest_disc_id}")
            else:
                logging.warning(f"⚠️ 评论媒体组返回 None")
        except FloodWaitError as fwe:
            logging.warning(f"⛔ FloodWait (评论组): {fwe.seconds} 秒")
            await asyncio.sleep(fwe.seconds + 10)
            try:
                fwded = await send_message(
                    dest_disc_id, tm_template,
                    grouped_messages=[tm.message for tm in tms],
                    grouped_tms=tms, comment_to_post=dest_top_id,
                )
                if fwded:
                    logging.info(f"💬 评论媒体组重试成功")
            except Exception as e2:
                logging.error(f"❌ 评论媒体组重试失败: {e2}")
        except Exception as e:
            logging.error(f"❌ 评论媒体组失败: {e}")

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

        for forward in CONFIG.forwards:
            if not forward.use_this:
                continue
            sources = config.get_forward_sources(forward)
            if not sources:
                continue
            dest = []
            for d in forward.dest:
                dest_resolved = d
                if not isinstance(dest_resolved, int):
                    try:
                        dest_resolved = await config.get_id(client, d)
                    except Exception:
                        continue
                dest.append(dest_resolved)
            if not dest:
                continue

            for source in sources:
                src = source
                if not isinstance(src, int):
                    try:
                        src = await config.get_id(client, source)
                    except Exception:
                        continue
                offset_key = str(src)
                last_id = 0
                grouped_buffer: Dict[int, List[Message]] = defaultdict(list)
                prev_grouped_id: Optional[int] = None
                start_offset = forward.offsets.get(offset_key, forward.offset)

                async for message in client.iter_messages(src, reverse=True, offset_id=start_offset):
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
                                logging.warning(f"⛔ FloodWait (组刷新): {fwe.seconds} 秒")
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

                        bot_media = []
                        bot_media_allowed = _bot_media_allowed(forward)
                        auto_comment_allowed = (forward is None or forward.auto_comment_trigger_enabled is not False)
                        if bot_media_allowed and auto_comment_allowed:
                            keyword = _extract_comment_keyword(message.raw_text or message.text or "", forward)
                            if keyword:
                                await _auto_comment_keyword(client, src, message.id, keyword)

                        comment_bot_media = await _collect_bot_media_from_comments(client, src, message.id, forward)
                        if comment_bot_media:
                            combined_messages = [message] + comment_bot_media
                            tms = await apply_plugins_to_group(combined_messages)
                            if tms:
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
                                    try:
                                        tms[0].reply_to = reply_to_id
                                        fwded_msg = await send_message(
                                            d,
                                            tms[0],
                                            grouped_messages=[tm.message for tm in tms],
                                            grouped_tms=tms,
                                        )
                                        if fwded_msg is not None:
                                            st.stored[event_uid][d] = fwded_msg
                                            fwded_id = _extract_msg_id(fwded_msg)
                                            if fwded_id is not None:
                                                st.add_post_mapping(src, message.id, d, fwded_id)
                                    except Exception as e:
                                        logging.error(f"❌ 合并媒体发送失败: {e}")
                                for tm in tms:
                                    tm.clear()
                            else:
                                logging.warning("⚠️ 合并媒体组全部被插件过滤，跳过")
                            last_id = message.id
                            forward.offsets[offset_key] = last_id
                            if len(sources) == 1:
                                forward.offset = last_id
                            write_config(CONFIG, persist=False)
                        else:
                            if bot_media_allowed:
                                bot_media = await resolve_bot_media_from_message(client, message, forward)
                            if bot_media:
                                bot_media = _dedupe_messages(bot_media)
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
                                                st.add_post_mapping(src, message.id, d, fwded_id)
                                    except Exception as e:
                                        logging.error(f"❌ bot 媒体发送失败: {e}")
                                last_id = message.id
                                forward.offsets[offset_key] = last_id
                                if len(sources) == 1:
                                    forward.offset = last_id
                                write_config(CONFIG, persist=False)
                            else:
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
                                forward.offsets[offset_key] = last_id
                                if len(sources) == 1:
                                    forward.offset = last_id
                                write_config(CONFIG, persist=False)

                        if forward.comments.enabled:
                            try:
                                await _forward_comments_for_post(client, src, message.id, forward)
                            except Exception as e:
                                logging.error(f"❌ 帖子 {message.id} 评论转发失败: {e}")

                        delay_seconds = random.randint(60, 300)
                        logging.info(f"⏸️ 休息 {delay_seconds} 秒 (消息 {message.id})")
                        await asyncio.sleep(delay_seconds)

                    except FloodWaitError as fwe:
                        logging.warning(f"⛔ FloodWait: {fwe.seconds} 秒")
                        await asyncio.sleep(fwe.seconds)
                    except Exception as err:
                        logging.exception(err)

                if grouped_buffer:
                    logging.info(f"📦 刷新剩余 {len(grouped_buffer)} 个媒体组")
                    try:
                        await _flush_grouped_buffer(client, src, dest, grouped_buffer, forward)
                    except Exception as e:
                        logging.exception(f"🚨 刷新剩余组失败: {e}")
