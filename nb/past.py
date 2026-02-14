import asyncio
import logging
import random
from collections import defaultdict
from typing import List, Dict, Optional

from telethon import TelegramClient
from telethon.errors.rpcerrorlist import FloodWaitError
from telethon.tl.custom.message import Message
from telethon.tl.patched import MessageService
from telethon.tl.types import MessageMediaGame, MessageMediaPoll, MessageMediaDice

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
    """从转发结果中提取消息 ID"""
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


def _is_unsupported_media(message: Message) -> bool:
    """检测不支持转发的媒体类型"""
    if not message or not message.media:
        return False
    
    media = message.media
    
    # 游戏消息：用户账号无法发送
    if isinstance(media, MessageMediaGame):
        return True
    
    # 投票消息：需要特殊处理
    if isinstance(media, MessageMediaPoll):
        return True
    
    # 骰子消息
    if isinstance(media, MessageMediaDice):
        return True
    
    return False


def _get_media_type_name(message: Message) -> str:
    """获取媒体类型名称用于日志"""
    if not message or not message.media:
        return "text"
    
    media = message.media
    media_type = type(media).__name__
    return media_type.replace("MessageMedia", "").lower()


async def _get_comments_method_a(
    client: TelegramClient,
    channel_id,
    msg_id: int,
) -> List[Message]:
    """方法A: 直接通过 reply_to 获取评论"""
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
    """方法B: 通过讨论组获取评论"""
    comments = []
    try:
        disc_msg = await get_discussion_message(client, channel_id, msg_id)
        if disc_msg is None:
            return comments

        discussion_id = disc_msg.chat_id
        top_id = disc_msg.id

        # 记录映射
        st.add_discussion_post_mapping(discussion_id, top_id, msg_id)

        async for msg in client.iter_messages(
            discussion_id,
            reply_to=top_id,
            reverse=True,
        ):
            comments.append(msg)

        if comments:
            logging.info(f"💬 方法B: 获取到 {len(comments)} 条评论")
    except Exception as e:
        logging.warning(f"⚠️ 方法B失败: {e}")
    return comments


async def _get_comments_method_c(
    client: TelegramClient,
    channel_id,
    msg_id: int,
) -> List[Message]:
    """方法C: 通过 min_id 范围获取"""
    comments = []
    try:
        disc_msg = await get_discussion_message(client, channel_id, msg_id)
        if disc_msg is None:
            return comments

        discussion_id = disc_msg.chat_id
        top_id = disc_msg.id

        st.add_discussion_post_mapping(discussion_id, top_id, msg_id)

        async for msg in client.iter_messages(
            discussion_id,
            min_id=top_id,
            reverse=True,
            limit=500,
        ):
            if msg.id == top_id:
                continue

            reply_to = getattr(msg, 'reply_to', None)
            if reply_to is None:
                continue

            msg_reply_to = getattr(reply_to, 'reply_to_msg_id', None)
            msg_top_id = getattr(reply_to, 'reply_to_top_id', None)

            if msg_top_id == top_id or msg_reply_to == top_id:
                comments.append(msg)

        if comments:
            logging.info(f"💬 方法C: 获取到 {len(comments)} 条评论")
    except Exception as e:
        logging.warning(f"⚠️ 方法C失败: {e}")
    return comments


async def _get_all_comments(
    client: TelegramClient,
    channel_id,
    msg_id: int,
    retry_delay: int = 3,
) -> List[Message]:
    """综合获取评论的方法"""
    comments = await _get_comments_method_a(client, channel_id, msg_id)
    if comments:
        return comments

    comments = await _get_comments_method_b(client, channel_id, msg_id)
    if comments:
        return comments

    if retry_delay > 0:
        logging.info(f"💬 方法A/B均未获取到评论，等待 {retry_delay}s 后重试...")
        await asyncio.sleep(retry_delay)
        comments = await _get_comments_method_a(client, channel_id, msg_id)
        if comments:
            return comments

    comments = await _get_comments_method_c(client, channel_id, msg_id)
    return comments


def _group_comments(comments: List[Message]) -> List[List[Message]]:
    """将评论按媒体组分组"""
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


async def _send_past_grouped(
    client: TelegramClient, src: int, dest: List[int], messages: List[Message]
) -> Dict[int, Optional[int]]:
    """发送媒体组并返回每个目标的发送结果"""
    tms = await apply_plugins_to_group(messages)
    if not tms:
        return {}

    tm_template = tms[0]
    if tm_template is None:
        return {}

    results = {}
    first_msg_id = messages[0].id

    for d in dest:
        try:
            # ★ 调试：媒体组发送前
            logging.info(
                f"📤 准备发送媒体组: src={src}, first_msg_id={first_msg_id}, "
                f"dest={d}, group_size={len(messages)}"
            )
            
            fwded_msgs = await send_message(
                d, tm_template,
                grouped_messages=[tm.message for tm in tms],
                grouped_tms=tms,
            )

            if fwded_msgs is not None:
                event_uid = st.EventUid(st.DummyEvent(src, first_msg_id))
                st.stored[event_uid] = {d: fwded_msgs}

                fwded_id = _extract_msg_id(fwded_msgs)
                if fwded_id is not None:
                    st.add_post_mapping(src, first_msg_id, d, fwded_id)
                    results[d] = fwded_id
                    logging.info(
                        f"✅ 媒体组映射建立: ({src}, {first_msg_id}) → ({d}, {fwded_id})"
                    )
                else:
                    results[d] = None
                    logging.warning(f"⚠️ 媒体组发送成功但无法提取 ID")
            else:
                results[d] = None
                logging.warning(f"⚠️ 媒体组发送返回 None")

        except Exception as e:
            logging.error(f"🚨 组播失败 ({src} → {d}): {e}")
            results[d] = None

    return results


async def _flush_grouped_buffer(
    client: TelegramClient,
    src: int,
    dest: List[int],
    grouped_buffer: Dict[int, List[Message]],
    forward,
) -> int:
    """刷新缓冲区中的媒体组"""
    last_id = 0
    for gid, msgs in list(grouped_buffer.items()):
        if not msgs:
            continue

        results = await _send_past_grouped(client, src, dest, msgs)

        group_last_id = max(m.id for m in msgs)
        last_id = max(last_id, group_last_id)

        forward.offset = group_last_id
        write_config(CONFIG, persist=False)

        # 检查是否有成功的发送
        success_count = sum(1 for v in results.values() if v is not None)
        
        delay_seconds = random.randint(60, 300)
        logging.info(
            f"✅ 媒体组 {gid} ({len(msgs)} 条) 完成, "
            f"成功 {success_count}/{len(dest)}, "
            f"offset → {group_last_id}, 休息 {delay_seconds}s"
        )
        await asyncio.sleep(delay_seconds)

    grouped_buffer.clear()
    return last_id


async def _forward_comments_for_post(
    client: TelegramClient,
    src_channel_id: int,
    src_post_id: int,
    dest_list: List[int],
    forward: config.Forward,
) -> None:
    """转发帖子的评论"""
    comments_cfg = forward.comments

    logging.info(f"💬 ═══ 开始处理帖子 {src_post_id} 的评论 ═══")

    await asyncio.sleep(2)

    comments = await _get_all_comments(
        client, src_channel_id, src_post_id, retry_delay=5
    )

    if not comments:
        logging.info(f"💬 帖子 {src_post_id} 没有评论")
        return

    logging.info(f"💬 帖子 {src_post_id}: 原始评论 {len(comments)} 条")

    # 过滤评论
    filtered = []
    for comment in comments:
        if isinstance(comment, MessageService):
            continue

        # 跳过帖子副本（频道消息的转发）
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
        logging.info(f"💬 帖子 {src_post_id}: 全部被过滤")
        return

    send_units = _group_comments(filtered)
    single_count = sum(1 for u in send_units if len(u) == 1)
    group_count = sum(1 for u in send_units if len(u) > 1)
    logging.info(
        f"💬 帖子 {src_post_id}: {len(filtered)} 条 → "
        f"{len(send_units)} 单元 ({single_count} 单条 + {group_count} 组)"
    )

    # 构建评论目标
    dest_targets = {}

    for dest_ch in dest_list:
        dest_resolved = dest_ch
        if not isinstance(dest_resolved, int):
            try:
                dest_resolved = await config.get_id(client, dest_ch)
            except Exception:
                continue

        # 使用增强的映射查找（支持多种 ID 格式）
        dest_post_id = st.get_dest_post_id(
            src_channel_id, src_post_id, dest_resolved
        )
        
        if dest_post_id is None:
            # ★ 详细的调试输出
            logging.warning(
                f"⚠️ 帖子 {src_post_id} → 目标 {dest_resolved}: 没有帖子映射\n"
                f"   源: {src_channel_id}, 目标: {dest_resolved}\n"
                f"   当前映射数量: {len(st.post_id_mapping)}"
            )
            # 打印前5个映射用于调试
            if st.post_id_mapping:
                sample_mappings = list(st.post_id_mapping.items())[:5]
                for (src_ch, src_msg), dest_map in sample_mappings:
                    logging.debug(f"   样本映射: ({src_ch}, {src_msg}) → {dest_map}")
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
                        f"disc_chat={dest_disc.chat_id}, disc_msg={dest_disc.id}"
                    )
                else:
                    # 回退：直接回复频道帖子
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

    sent_count = 0
    fail_count = 0

    for unit_idx, unit_msgs in enumerate(send_units):
        is_group = len(unit_msgs) > 1

        if is_group:
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
                        # 使用源讨论组 ID（不是源频道 ID）
                        src_discussion_id = unit_msgs[0].chat_id
                        fwded_id = _extract_msg_id(fwded)
                        if fwded_id:
                            st.add_comment_mapping(
                                src_discussion_id, unit_msgs[0].id,
                                dest_chat_id, fwded_id,
                            )
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
                        # 使用评论所在的讨论组 ID
                        src_discussion_id = comment.chat_id
                        fwded_id = _extract_msg_id(fwded)
                        if fwded_id:
                            st.add_comment_mapping(
                                src_discussion_id, comment.id,
                                dest_chat_id, fwded_id,
                            )
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

        delay = random.randint(5, 20)
        await asyncio.sleep(delay)

    logging.info(
        f"💬 ═══ 帖子 {src_post_id} 评论完成: "
        f"成功={sent_count} 失败={fail_count} ═══"
    )


async def forward_job() -> None:
    """past 模式主函数"""
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
            logging.error("❌ Bot 账号无法用 past 模式")
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

        # 建立源频道 ID → Forward 对象的映射
        resolved_forwards: Dict[int, config.Forward] = {}
        for forward in CONFIG.forwards:
            if not forward.use_this:
                continue
            src = forward.source
            if not isinstance(src, int) and str(src).strip() == "":
                continue
            try:
                src_id = await config.get_id(client, forward.source)
                resolved_forwards[src_id] = forward
                
                # 同时保存标准化格式的映射
                normalized_id = st._normalize_channel_id(src_id)
                if normalized_id != src_id:
                    resolved_forwards[normalized_id] = forward
            except Exception as e:
                logging.error(f"❌ 无法解析源 {forward.source}: {e}")
                continue

        for src, dest in config.from_to.items():
            forward = resolved_forwards.get(src)
            if forward is None:
                # 尝试用标准化 ID 查找
                normalized_src = st._normalize_channel_id(src)
                forward = resolved_forwards.get(normalized_src)
            
            if forward is None:
                logging.warning(f"⚠️ 找不到 src={src} 对应的 Forward 配置，跳过")
                continue

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

                    # 处理媒体组边界
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

                    # 媒体组消息：添加到缓冲区
                    if current_gid is not None:
                        grouped_buffer[current_gid].append(message)
                        continue

                    # 检测不支持的媒体类型
                    if _is_unsupported_media(message):
                        media_type = _get_media_type_name(message)
                        logging.warning(
                            f"⚠️ 跳过不支持的媒体类型 (msg={message.id}): {media_type}"
                        )
                        
                        # ★ 即使媒体不支持，也要尝试只发送文字（如果有的话）
                        if message.text and message.text.strip():
                            try:
                                for d in dest:
                                    fallback_msg = await client.send_message(
                                        d,
                                        f"[原消息包含 {media_type}，无法转发]\n\n{message.text}"
                                    )
                                    if fallback_msg:
                                        # 建立映射，以便评论可以找到
                                        st.add_post_mapping(src, message.id, d, fallback_msg.id)
                                        logging.info(
                                            f"✅ 替代文本发送成功并建立映射: "
                                            f"({src}, {message.id}) → ({d}, {fallback_msg.id})"
                                        )
                            except Exception as e:
                                logging.error(f"❌ 替代文本发送失败: {e}")
                        
                        forward.offset = message.id
                        write_config(CONFIG, persist=False)
                        continue

                    # 应用插件
                    tm = await apply_plugins(message)
                    if not tm:
                        forward.offset = message.id
                        write_config(CONFIG, persist=False)
                        continue

                    event_uid = st.EventUid(
                        st.DummyEvent(message.chat_id, message.id)
                    )
                    st.stored[event_uid] = {}

                    # 记录每个目标的发送结果
                    any_success = False
                    
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
                            # ★★★ 调试：打印发送前信息 ★★★
                            logging.info(
                                f"📤 准备发送: src={src}, msg_id={message.id}, dest={d}, "
                                f"media_type={tm.file_type}, has_text={bool(tm.text)}"
                            )
                            
                            fwded_msg = await send_message(d, tm)
                            
                            # ★★★ 调试：打印发送结果 ★★★
                            if fwded_msg is None:
                                logging.warning(
                                    f"⚠️ send_message 返回 None: "
                                    f"src={src}, msg_id={message.id}, dest={d}"
                                )
                            else:
                                logging.info(
                                    f"📨 发送成功: type={type(fwded_msg).__name__}, "
                                    f"id={_extract_msg_id(fwded_msg)}"
                                )
                            
                            if fwded_msg is not None:
                                st.stored[event_uid][d] = fwded_msg
                                fwded_id = _extract_msg_id(fwded_msg)
                                if fwded_id is not None:
                                    # ★ 关键：建立帖子映射
                                    st.add_post_mapping(src, message.id, d, fwded_id)
                                    any_success = True
                                    
                                    # ★ 验证映射是否建立成功
                                    verify = st.get_dest_post_id(src, message.id, d)
                                    if verify == fwded_id:
                                        logging.info(
                                            f"✅ 映射验证成功: src({src}, {message.id}) → dest({d}, {verify})"
                                        )
                                    else:
                                        logging.error(
                                            f"❌ 映射验证失败: 期望 {fwded_id}, 实际 {verify}"
                                        )
                                else:
                                    logging.warning(
                                        f"⚠️ 无法提取消息 ID: fwded_msg={fwded_msg}"
                                    )
                                    
                        except Exception as e:
                            logging.error(
                                f"❌ 发送异常: {src}/{message.id} → {d}: {e}",
                                exc_info=True
                            )

                    tm.clear()
                    last_id = message.id
                    forward.offset = last_id
                    write_config(CONFIG, persist=False)

                    # 只有主帖发送成功才处理评论
                    if forward.comments.enabled and any_success:
                        logging.info(f"💬 准备转发帖子 {message.id} 的评论...")
                        try:
                            await _forward_comments_for_post(
                                client, src, message.id, dest, forward
                            )
                        except Exception as e:
                            logging.error(
                                f"❌ 帖子 {message.id} 评论失败: {e}",
                                exc_info=True,
                            )
                    elif forward.comments.enabled and not any_success:
                        logging.warning(
                            f"⚠️ 帖子 {message.id} 发送失败，跳过评论转发"
                        )

                    # 延迟
                    delay = CONFIG.past.delay if CONFIG.past.delay > 0 else random.randint(60, 300)
                    logging.info(f"⏸️ 休息 {delay}s (msg {message.id})")
                    await asyncio.sleep(delay)

                except FloodWaitError as fwe:
                    logging.warning(f"⛔ FloodWait: {fwe.seconds}s")
                    await asyncio.sleep(fwe.seconds)
                except Exception as err:
                    logging.exception(f"处理消息 {message.id} 时出错: {err}")

            # 处理剩余的媒体组
            if grouped_buffer:
                try:
                    await _flush_grouped_buffer(
                        client, src, dest, grouped_buffer, forward
                    )
                except Exception as e:
                    logging.exception(f"🚨 刷新剩余组失败: {e}")

        logging.info("🏁 past 模式完成")
        
        # ★ 最后输出映射统计
        logging.info(f"📊 最终统计: 共建立 {len(st.post_id_mapping)} 个帖子映射")
