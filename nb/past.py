import asyncio
import logging
import random
from collections import defaultdict
from typing import Dict, List, Optional, Tuple
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


async def _get_all_comments(client, channel_id: int, post_id: int) -> List[Message]:
    """获取帖子的所有评论 - 多种方法尝试"""
    comments = []
    
    # 方法1: 直接通过 GetDiscussionMessage API
    logging.info(f"尝试获取评论: 频道={channel_id}, 帖子={post_id}")
    
    try:
        disc_msg = await get_discussion_message(client, channel_id, post_id)
        if disc_msg:
            discussion_id = disc_msg.chat_id
            top_msg_id = disc_msg.id
            logging.info(f"找到讨论消息: 讨论组={discussion_id}, 消息ID={top_msg_id}")
            
            # 获取所有回复
            async for msg in client.iter_messages(discussion_id, reply_to=top_msg_id, reverse=True):
                if not isinstance(msg, MessageService):
                    comments.append(msg)
            
            if comments:
                logging.info(f"方法1获取到 {len(comments)} 条评论")
                return comments
    except Exception as e:
        logging.debug(f"方法1失败: {e}")
    
    # 方法2: 通过讨论组ID直接搜索
    try:
        dg_id = await get_discussion_group_id(client, channel_id)
        if dg_id:
            logging.info(f"尝试讨论组 {dg_id}")
            
            # 先找到帖子头
            header_msg_id = None
            async for msg in client.iter_messages(dg_id, limit=200):
                cp = _extract_channel_post(msg)
                if cp == post_id:
                    header_msg_id = msg.id
                    st.add_discussion_mapping(dg_id, msg.id, cp)
                    break
            
            if header_msg_id:
                async for msg in client.iter_messages(dg_id, reply_to=header_msg_id, reverse=True):
                    if not isinstance(msg, MessageService):
                        comments.append(msg)
                
                if comments:
                    logging.info(f"方法2获取到 {len(comments)} 条评论")
                    return comments
    except Exception as e:
        logging.debug(f"方法2失败: {e}")
    
    # 方法3: 直接从频道获取评论
    try:
        async for msg in client.iter_messages(channel_id, reply_to=post_id, reverse=True):
            if not isinstance(msg, MessageService):
                comments.append(msg)
        
        if comments:
            logging.info(f"方法3获取到 {len(comments)} 条评论")
            return comments
    except Exception as e:
        logging.debug(f"方法3失败: {e}")
    
    logging.info(f"帖子 {post_id} 没有找到评论")
    return []


def _group_comments_by_media(comments: List[Message]) -> List[List[Message]]:
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


async def _forward_single_comment(client, comment: Message, dest_chat_id: int, reply_to_id: Optional[int]) -> bool:
    """转发单条评论"""
    try:
        tm = await apply_plugins(comment)
        if not tm:
            return False
        
        fwded = await send_message(dest_chat_id, tm, comment_to_post=reply_to_id)
        tm.clear()
        
        if fwded:
            st.add_comment_mapping(comment.chat_id, comment.id, dest_chat_id, extract_msg_id(fwded))
            return True
        return False
    except Exception as e:
        logging.error(f"评论发送失败: {e}")
        return False


async def _forward_grouped_comments(client, comments: List[Message], dest_chat_id: int, reply_to_id: Optional[int]) -> bool:
    """转发媒体组评论"""
    try:
        tms = await apply_plugins_to_group(comments)
        if not tms or not tms[0]:
            return False
        
        fwded = await send_message(
            dest_chat_id, tms[0],
            grouped_messages=[tm.message for tm in tms],
            grouped_tms=tms,
            comment_to_post=reply_to_id
        )
        
        for tm in tms:
            tm.clear()
        
        if fwded:
            st.add_comment_mapping(comments[0].chat_id, comments[0].id, dest_chat_id, extract_msg_id(fwded))
            return True
        return False
    except Exception as e:
        logging.error(f"媒体组评论发送失败: {e}")
        return False


async def _forward_comments_for_post(
    client,
    src_channel_id: int,
    src_post_id: int,
    dest_channels: List[int],
    forward_config
) -> int:
    """转发指定帖子的所有评论"""
    cfg = forward_config.comments
    forwarded_count = 0
    
    # 获取评论
    all_comments = await _get_all_comments(client, src_channel_id, src_post_id)
    if not all_comments:
        return 0
    
    # 过滤评论
    filtered = []
    for c in all_comments:
        # 跳过帖子头（自动转发的消息）
        if _extract_channel_post(c):
            continue
        # 仅媒体
        if cfg.only_media and not c.media:
            continue
        # 包含文本
        if not cfg.include_text_comments and not c.media:
            continue
        # 跳过机器人
        if cfg.skip_bot_comments:
            try:
                sender = await c.get_sender()
                if sender and getattr(sender, 'bot', False):
                    continue
            except:
                pass
        filtered.append(c)
    
    if not filtered:
        logging.info(f"帖子 {src_post_id}: 过滤后无有效评论")
        return 0
    
    logging.info(f"帖子 {src_post_id}: 准备转发 {len(filtered)} 条评论")
    
    # 分组
    units = _group_comments_by_media(filtered)
    
    # 为每个目标频道转发
    for dest_channel in dest_channels:
        dest_resolved = dest_channel
        if not isinstance(dest_resolved, int):
            try:
                dest_resolved = await config.get_id(client, dest_channel)
            except:
                continue
        
        # 获取目标帖子ID
        dest_post_id = st.get_dest_post_id(src_channel_id, src_post_id, dest_resolved)
        if not dest_post_id:
            logging.warning(f"❌ 映射不存在: {src_channel_id}/{src_post_id} -> {dest_resolved}")
            continue
        
        logging.info(f"目标映射: {src_channel_id}/{src_post_id} -> {dest_resolved}/{dest_post_id}")
        
        # 确定回复位置
        dest_reply_to = None
        if cfg.dest_mode == "comments":
            disc_msg = await get_discussion_message(client, dest_resolved, dest_post_id)
            if disc_msg:
                dest_chat_id = disc_msg.chat_id
                dest_reply_to = disc_msg.id
                logging.info(f"目标讨论组: {dest_chat_id}/{dest_reply_to}")
            else:
                dest_chat_id = dest_resolved
                dest_reply_to = dest_post_id
                logging.info(f"直接回复频道: {dest_chat_id}/{dest_reply_to}")
        else:
            # discussion 模式
            dest_chat_id = dest_resolved
            dest_reply_to = dest_post_id
        
        # 转发每个单元
        for unit in units:
            success = False
            if len(unit) > 1:
                success = await _forward_grouped_comments(client, unit, dest_chat_id, dest_reply_to)
            else:
                success = await _forward_single_comment(client, unit[0], dest_chat_id, dest_reply_to)
            
            if success:
                forwarded_count += 1
                logging.info(f"✅ 评论转发成功 -> {dest_chat_id}")
            
            # 短暂延迟避免flood
            await asyncio.sleep(random.uniform(1, 3))
    
    return forwarded_count


async def _forward_single_post(
    client,
    message: Message,
    src_channel_id: int,
    dest_channels: List[int],
    forward_config
) -> bool:
    """转发单条主帖子"""
    tm = await apply_plugins(message)
    if not tm:
        return False
    
    uid = st.EventUid(st.DummyEvent(message.chat_id, message.id))
    st.stored[uid] = {}
    success = False
    
    for dest in dest_channels:
        dest_resolved = dest
        if not isinstance(dest_resolved, int):
            try:
                dest_resolved = await config.get_id(client, dest)
            except:
                continue
        
        # 处理回复
        reply_to_id = None
        if message.is_reply:
            rmid = _get_reply_to_msg_id(message)
            if rmid:
                r_uid = st.EventUid(st.DummyEvent(message.chat_id, rmid))
                if r_uid in st.stored:
                    fr = st.stored[r_uid].get(dest_resolved)
                    reply_to_id = fr if isinstance(fr, int) else getattr(fr, 'id', None) if fr else None
        
        tm.reply_to = reply_to_id
        
        try:
            fwded = await send_message(dest_resolved, tm)
            if fwded:
                st.stored[uid][dest_resolved] = fwded
                fid = extract_msg_id(fwded)
                if fid:
                    st.add_post_mapping(src_channel_id, message.id, dest_resolved, fid)
                    success = True
        except Exception as e:
            logging.error(f"发送失败 -> {dest_resolved}: {e}")
    
    tm.clear()
    return success


async def _forward_grouped_posts(
    client,
    messages: List[Message],
    src_channel_id: int,
    dest_channels: List[int],
    forward_config
) -> bool:
    """转发媒体组帖子"""
    tms = await apply_plugins_to_group(messages)
    if not tms or not tms[0]:
        return False
    
    first_msg = messages[0]
    uid = st.EventUid(st.DummyEvent(first_msg.chat_id, first_msg.id))
    st.stored[uid] = {}
    success = False
    
    for dest in dest_channels:
        dest_resolved = dest
        if not isinstance(dest_resolved, int):
            try:
                dest_resolved = await config.get_id(client, dest)
            except:
                continue
        
        try:
            fwded = await send_message(
                dest_resolved, tms[0],
                grouped_messages=[tm.message for tm in tms],
                grouped_tms=tms
            )
            if fwded:
                st.stored[uid][dest_resolved] = fwded
                fid = extract_msg_id(fwded[0] if isinstance(fwded, list) else fwded)
                if fid:
                    st.add_post_mapping(src_channel_id, first_msg.id, dest_resolved, fid)
                    success = True
        except Exception as e:
            logging.error(f"媒体组发送失败 -> {dest_resolved}: {e}")
    
    for tm in tms:
        tm.clear()
    
    return success


async def forward_job():
    """Past模式主入口"""
    logging.info("=" * 50)
    logging.info("启动 Past 模式")
    logging.info("=" * 50)
    
    clean_session_files()
    await load_async_plugins()
    
    if CONFIG.login.user_type != 1:
        logging.error("❌ Past模式需要User账号 (user_type=1)")
        return
    if not CONFIG.login.SESSION_STRING:
        logging.error("❌ SESSION_STRING未设置")
        return
    
    SESSION = get_SESSION()
    
    async with TelegramClient(SESSION, CONFIG.login.API_ID, CONFIG.login.API_HASH) as client:
        if await client.is_bot():
            logging.error("❌ Bot账号不支持Past模式")
            return
        
        # 加载转发配置
        config.from_to = await config.load_from_to(client, CONFIG.forwards)
        if not config.from_to:
            logging.error("❌ 没有有效的转发配置")
            return
        
        # 解析所有转发配置
        resolved_forwards: Dict[int, config.Forward] = {}
        for forward in CONFIG.forwards:
            if not forward.use_this:
                continue
            try:
                src_id = await config.get_id(client, forward.source)
                resolved_forwards[src_id] = forward
                logging.info(f"转发配置: {forward.source} ({src_id}) -> {forward.dest}")
            except Exception as e:
                logging.warning(f"解析源失败 {forward.source}: {e}")
        
        # 处理每个源频道
        for src_channel_id, dest_channels in config.from_to.items():
            forward = resolved_forwards.get(src_channel_id)
            if not forward:
                continue
            
            logging.info(f"\n{'='*40}")
            logging.info(f"处理频道: {src_channel_id}")
            logging.info(f"目标: {dest_channels}")
            logging.info(f"偏移: {forward.offset}")
            logging.info(f"评论: {'启用' if forward.comments.enabled else '禁用'}")
            logging.info(f"{'='*40}")
            
            # 预加载讨论组映射
            if forward.comments.enabled:
                try:
                    dg_id = await get_discussion_group_id(client, src_channel_id)
                    if dg_id:
                        count = await preload_discussion_mappings(client, dg_id, limit=1000)
                        logging.info(f"预加载 {count} 个讨论映射")
                except Exception as e:
                    logging.warning(f"预加载失败: {e}")
            
            # 媒体组缓冲区
            grouped_buffer: Dict[int, List[Message]] = defaultdict(list)
            post_count = 0
            comment_count = 0
            
            # 遍历消息
            async for message in client.iter_messages(src_channel_id, reverse=True, offset_id=forward.offset):
                if isinstance(message, MessageService):
                    continue
                if forward.end and message.id > forward.end:
                    logging.info(f"到达结束位置: {forward.end}")
                    break
                
                try:
                    current_gid = message.grouped_id
                    
                    # 处理之前的媒体组
                    if grouped_buffer and (current_gid is None or current_gid not in grouped_buffer):
                        for gid, msgs in list(grouped_buffer.items()):
                            if msgs:
                                logging.info(f"发送媒体组: {len(msgs)} 张")
                                success = await _forward_grouped_posts(client, msgs, src_channel_id, dest_channels, forward)
                                if success:
                                    post_count += 1
                                    # 转发评论
                                    if forward.comments.enabled:
                                        first_id = msgs[0].id
                                        cc = await _forward_comments_for_post(
                                            client, src_channel_id, first_id, dest_channels, forward
                                        )
                                        comment_count += cc
                                
                                forward.offset = max(m.id for m in msgs)
                                write_config(CONFIG, persist=False)
                                await asyncio.sleep(random.randint(30, 120))
                        grouped_buffer.clear()
                    
                    # 当前消息是媒体组的一部分
                    if current_gid is not None:
                        grouped_buffer[current_gid].append(message)
                        continue
                    
                    # 单条消息
                    logging.info(f"处理消息: {message.id}")
                    success = await _forward_single_post(client, message, src_channel_id, dest_channels, forward)
                    
                    if success:
                        post_count += 1
                        
                        # 转发评论
                        if forward.comments.enabled:
                            cc = await _forward_comments_for_post(
                                client, src_channel_id, message.id, dest_channels, forward
                            )
                            comment_count += cc
                    
                    forward.offset = message.id
                    write_config(CONFIG, persist=False)
                    
                    await asyncio.sleep(random.randint(30, 120))
                    
                except FloodWaitError as fwe:
                    logging.warning(f"FloodWait: {fwe.seconds}秒")
                    await asyncio.sleep(fwe.seconds + 10)
                except Exception as e:
                    logging.exception(f"处理消息 {message.id} 出错: {e}")
            
            # 处理剩余的媒体组
            for gid, msgs in grouped_buffer.items():
                if msgs:
                    logging.info(f"处理剩余媒体组: {len(msgs)} 张")
                    success = await _forward_grouped_posts(client, msgs, src_channel_id, dest_channels, forward)
                    if success:
                        post_count += 1
                        if forward.comments.enabled:
                            cc = await _forward_comments_for_post(
                                client, src_channel_id, msgs[0].id, dest_channels, forward
                            )
                            comment_count += cc
            
            logging.info(f"\n频道 {src_channel_id} 完成")
            logging.info(f"帖子: {post_count}, 评论: {comment_count}")
        
        logging.info("\n" + "=" * 50)
        logging.info("Past模式完成")
        logging.info("=" * 50)
