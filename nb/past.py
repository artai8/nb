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


# ======================== 评论获取 ========================

async def _get_all_comments(client, channel_id, post_id):
    """获取帖子的所有评论"""
    comments = []

    # 方法1: GetDiscussionMessage + reply_to
    try:
        disc_msg = await get_discussion_message(client, channel_id, post_id)
        if disc_msg:
            async for msg in client.iter_messages(
                disc_msg.chat_id, reply_to=disc_msg.id, reverse=True
            ):
                if not isinstance(msg, MessageService):
                    comments.append(msg)
            if comments:
                logging.info(f"方法1: 获取 {len(comments)} 条评论")
                return comments
    except Exception as e:
        logging.debug(f"方法1失败: {e}")

    # 方法2: 讨论组搜索
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
                    logging.info(f"方法2: 获取 {len(comments)} 条评论")
                    return comments
    except Exception as e:
        logging.debug(f"方法2失败: {e}")

    # 方法3: 直接从频道获取
    try:
        async for msg in client.iter_messages(
            channel_id, reply_to=post_id, reverse=True
        ):
            if not isinstance(msg, MessageService):
                comments.append(msg)
        if comments:
            logging.info(f"方法3: 获取 {len(comments)} 条评论")
            return comments
    except Exception as e:
        logging.debug(f"方法3失败: {e}")

    return []


def _group_comments_by_media(comments):
    """将评论按媒体组分组"""
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


# ======================== 评论转发 ========================

async def _forward_comments_for_post(client, src_channel_id, src_post_id, dest_list, forward):
    """转发指定帖子的所有评论"""
    cfg = forward.comments
    await asyncio.sleep(2)

    comments = await _get_all_comments(client, src_channel_id, src_post_id)
    if not comments:
        return 0

    # 过滤
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

    logging.info(f"帖子 {src_post_id}: 准备转发 {len(filtered)} 条评论")

    # 确定目标
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
            logging.warning(f"映射不存在: {src_channel_id}/{src_post_id} -> {dest_resolved}")
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
        logging.warning(f"没有有效评论目标")
        return 0

    # 分组发送
    units = _group_comments_by_media(filtered)
    forwarded = 0

    for unit in units:
        if len(unit) > 1:
            # 媒体组
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
                        st.add_comment_mapping(
                            src_channel_id, unit[0].id,
                            dest_chat_id, extract_msg_id(fwded),
                        )
                        forwarded += 1
                except Exception as e:
                    logging.error(f"评论媒体组失败: {e}")
            for tm in tms:
                tm.clear()
        else:
            # 单条
            comment = unit[0]
            tm = await apply_plugins(comment)
            if not tm:
                continue
            for dest_chat_id, dest_reply_to in dest_targets.items():
                try:
                    fwded = await send_message(
                        dest_chat_id, tm, comment_to_post=dest_reply_to
                    )
                    if fwded:
                        st.add_comment_mapping(
                            src_channel_id, comment.id,
                            dest_chat_id, extract_msg_id(fwded),
                        )
                        forwarded += 1
                except Exception as e:
                    logging.error(f"评论失败: {e}")
            tm.clear()

        await asyncio.sleep(random.uniform(1, 3))

    logging.info(f"帖子 {src_post_id}: 转发了 {forwarded} 条评论")
    return forwarded


# ======================== 主帖子处理 ========================

async def _forward_single_post(client, message, src, dest, forward):
    """转发单条主帖子"""
    tm = await apply_plugins(message)
    if not tm:
        return False

    uid = st.EventUid(st.DummyEvent(message.chat_id, message.id))
    st.stored[uid] = {}
    success = False

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
                    success = True
        except Exception as e:
            logging.error(f"发送失败 -> {d}: {e}")

    tm.clear()
    return success


async def _forward_grouped_posts(client, messages, src, dest, forward):
    """转发媒体组帖子"""
    tms = await apply_plugins_to_group(messages)
    if not tms or not tms[0]:
        return False

    first_msg = messages[0]
    uid = st.EventUid(st.DummyEvent(first_msg.chat_id, first_msg.id))
    st.stored[uid] = {}
    success = False

    for d in dest:
        try:
            fwded = await send_message(
                d, tms[0],
                grouped_messages=[tm.message for tm in tms],
                grouped_tms=tms,
            )
            if fwded:
                st.stored[uid][d] = fwded
                fid = extract_msg_id(
                    fwded[0] if isinstance(fwded, list) else fwded
                )
                if fid:
                    st.add_post_mapping(src, first_msg.id, d, fid)
                    success = True
        except Exception as e:
            logging.error(f"媒体组发送失败 -> {d}: {e}")

    for tm in tms:
        tm.clear()
    return success


# ======================== 主入口 ========================

async def forward_job():
    """Past模式主入口"""
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

            # 预加载讨论组映射
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

            async for message in client.iter_messages(
                src, reverse=True, offset_id=forward.offset
            ):
                if isinstance(message, MessageService):
                    continue
                if forward.end and message.id > forward.end:
                    break

                try:
                    current_gid = message.grouped_id

                    # 刷新之前的媒体组
                    if grouped_buffer and (
                        current_gid is None or current_gid not in grouped_buffer
                    ):
                        for gid, msgs in list(grouped_buffer.items()):
                            if not msgs:
                                continue
                            logging.info(f"发送媒体组: {len(msgs)} 张")
                            ok = await _forward_grouped_posts(client, msgs, src, dest, forward)
                            if ok:
                                post_count += 1
                                if forward.comments.enabled:
                                    cc = await _forward_comments_for_post(
                                        client, src, msgs[0].id, dest, forward
                                    )
                                    comment_count += cc
                            forward.offset = max(m.id for m in msgs)
                            write_config(CONFIG, persist=False)
                            await asyncio.sleep(random.randint(30, 120))
                        grouped_buffer.clear()

                    # 媒体组成员
                    if current_gid is not None:
                        grouped_buffer[current_gid].append(message)
                        continue

                    # 单条消息
                    logging.info(f"处理消息: {message.id}")
                    ok = await _forward_single_post(client, message, src, dest, forward)
                    if ok:
                        post_count += 1
                        if forward.comments.enabled:
                            cc = await _forward_comments_for_post(
                                client, src, message.id, dest, forward
                            )
                            comment_count += cc

                    forward.offset = message.id
                    write_config(CONFIG, persist=False)
                    await asyncio.sleep(random.randint(30, 120))

                except FloodWaitError as fwe:
                    logging.warning(f"FloodWait: {fwe.seconds}秒")
                    await asyncio.sleep(fwe.seconds + 10)
                except Exception as e:
                    logging.exception(f"处理消息出错: {e}")

            # 处理剩余媒体组
            for gid, msgs in grouped_buffer.items():
                if not msgs:
                    continue
                ok = await _forward_grouped_posts(client, msgs, src, dest, forward)
                if ok:
                    post_count += 1
                    if forward.comments.enabled:
                        cc = await _forward_comments_for_post(
                            client, src, msgs[0].id, dest, forward
                        )
                        comment_count += cc

            logging.info(f"频道 {src} 完成: 帖子={post_count}, 评论={comment_count}")

        logging.info("Past模式完成")
