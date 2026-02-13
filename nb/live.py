# 在文件开头导入部分添加
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple

# 评论处理相关全局变量
COMMENT_GROUPED_CACHE: Dict[int, Dict[int, List[Message]]] = {}
COMMENT_GROUPED_TIMERS: Dict[int, asyncio.TimerHandle] = {}
COMMENT_GROUPED_TIMEOUT = 3.0
PROCESSED_COMMENTS: Set[int] = set()  # 已处理的评论ID


async def _resolve_channel_post_id_enhanced(client, chat_id, message) -> Optional[int]:
    """增强版：解析评论所属的主帖子ID"""
    # 方法1：通过 reply_to_top_id
    top_id = _get_reply_to_top_id(message)
    if top_id is not None:
        cp = st.get_channel_post_id(chat_id, top_id)
        if cp:
            return cp
        # 尝试获取顶层消息
        try:
            top_msg = await client.get_messages(chat_id, ids=top_id)
            if top_msg:
                fwd = _extract_channel_post(top_msg)
                if fwd:
                    st.add_discussion_mapping(chat_id, top_id, fwd)
                    return fwd
        except Exception:
            pass
    
    # 方法2：通过 reply_to_msg_id
    reply_msg_id = _get_reply_to_msg_id(message)
    if reply_msg_id is not None:
        cp = st.get_channel_post_id(chat_id, reply_msg_id)
        if cp:
            return cp
        # 尝试获取回复的消息
        try:
            reply_msg = await client.get_messages(chat_id, ids=reply_msg_id)
            if reply_msg:
                fwd = _extract_channel_post(reply_msg)
                if fwd:
                    st.add_discussion_mapping(chat_id, reply_msg_id, fwd)
                    return fwd
                # 递归查找
                return await _resolve_channel_post_id_enhanced(client, chat_id, reply_msg)
        except Exception:
            pass
    
    # 方法3：向上遍历查找帖子头
    try:
        async for msg in client.iter_messages(chat_id, min_id=message.id - 50, max_id=message.id):
            if msg:
                fwd = _extract_channel_post(msg)
                if fwd:
                    st.add_discussion_mapping(chat_id, msg.id, fwd)
                    return fwd
    except Exception:
        pass
    
    return None


async def _find_or_create_dest_mapping(client, src_channel_id, src_post_id, dest_channel_id) -> Optional[int]:
    """查找或创建目标帖子映射"""
    # 首先检查现有映射
    existing = st.get_dest_post_id(src_channel_id, src_post_id, dest_channel_id)
    if existing:
        return existing
    
    # 尝试通过内容匹配查找
    try:
        # 获取源帖子
        src_msg = await client.get_messages(src_channel_id, ids=src_post_id)
        if not src_msg:
            return None
        
        # 在目标频道搜索相似内容
        search_text = (src_msg.text or "")[:50] if src_msg.text else None
        if search_text:
            async for dest_msg in client.iter_messages(dest_channel_id, search=search_text, limit=10):
                if dest_msg and dest_msg.text and search_text in dest_msg.text:
                    st.add_post_mapping(src_channel_id, src_post_id, dest_channel_id, dest_msg.id)
                    return dest_msg.id
        
        # 通过时间戳匹配（最近24小时内的消息）
        if src_msg.date:
            start_time = src_msg.date - timedelta(hours=1)
            end_time = src_msg.date + timedelta(hours=1)
            async for dest_msg in client.iter_messages(dest_channel_id, offset_date=end_time, limit=50):
                if dest_msg and dest_msg.date >= start_time:
                    # 比较内容相似度
                    if src_msg.text == dest_msg.text or (src_msg.media and dest_msg.media):
                        st.add_post_mapping(src_channel_id, src_post_id, dest_channel_id, dest_msg.id)
                        return dest_msg.id
    except Exception as e:
        logging.error(f"查找映射失败: {e}")
    
    return None


async def _process_comment_batch(client, comments: List[st.PendingComment]):
    """批量处理评论"""
    if not comments:
        return
    
    # 按目标分组
    by_dest = {}
    for comment in comments:
        forward = comment.forward
        src_channel_id = comment.source_channel_id
        src_post_id = comment.source_post_id
        
        for dest_ch in forward.dest:
            dest_resolved = dest_ch
            if not isinstance(dest_resolved, int):
                try:
                    dest_resolved = await config.get_id(client, dest_ch)
                except:
                    continue
            
            # 查找或创建映射
            dest_post_id = await _find_or_create_dest_mapping(client, src_channel_id, src_post_id, dest_resolved)
            if not dest_post_id:
                continue
            
            key = (dest_resolved, dest_post_id)
            if key not in by_dest:
                by_dest[key] = []
            by_dest[key].append(comment)
    
    # 批量发送
    for (dest_channel_id, dest_post_id), batch in by_dest.items():
        # 获取目标讨论组
        disc_msg = await get_discussion_message(client, dest_channel_id, dest_post_id)
        dest_chat_id = disc_msg.chat_id if disc_msg else dest_channel_id
        dest_reply_to = disc_msg.id if disc_msg else dest_post_id
        
        # 发送每条评论
        for comment in batch:
            if comment.message.id in PROCESSED_COMMENTS:
                continue
            
            try:
                # 处理媒体组
                if comment.grouped_id and comment.grouped_id in st.PENDING_COMMENT_GROUPS:
                    messages = st.PENDING_COMMENT_GROUPS[comment.grouped_id]
                    tms = await apply_plugins_to_group(messages)
                    if tms:
                        await send_message(
                            dest_chat_id, tms[0],
                            grouped_messages=[tm.message for tm in tms],
                            grouped_tms=tms,
                            comment_to_post=dest_reply_to
                        )
                        for tm in tms:
                            tm.clear()
                    st.PENDING_COMMENT_GROUPS.pop(comment.grouped_id, None)
                else:
                    # 单条评论
                    tm = await apply_plugins(comment.message)
                    if tm:
                        await send_message(dest_chat_id, tm, comment_to_post=dest_reply_to)
                        tm.clear()
                
                PROCESSED_COMMENTS.add(comment.message.id)
                st.add_comment_mapping(
                    comment.message.chat_id,
                    comment.message.id,
                    dest_chat_id,
                    dest_reply_to
                )
            except Exception as e:
                logging.error(f"评论发送失败: {e}")


async def comment_message_handler(event):
    """重写的评论处理器"""
    chat_id = event.chat_id
    message = event.message
    
    # 检查是否在监听的讨论组中
    if chat_id not in config.comment_sources:
        return
    
    forward = config.comment_forward_map.get(chat_id)
    if not forward or not forward.comments.enabled:
        return
    
    # 检查是否是帖子头消息
    cp = _extract_channel_post(message)
    if cp:
        st.add_discussion_mapping(chat_id, message.id, cp)
        logging.info(f"发现帖子头: 讨论组={chat_id}/{message.id} -> 频道帖子={cp}")
        return
    
    # 过滤
    if forward.comments.only_media and not message.media:
        return
    if not forward.comments.include_text_comments and not message.media:
        return
    if forward.comments.skip_bot_comments:
        try:
            sender = await event.get_sender()
            if sender and getattr(sender, 'bot', False):
                return
        except:
            pass
    
    # 解析所属主帖子
    src_post_id = await _resolve_channel_post_id_enhanced(event.client, chat_id, message)
    if not src_post_id:
        logging.warning(f"无法解析评论所属帖子: chat={chat_id} msg={message.id}")
        # 尝试加入延迟队列（假设主帖子稍后会出现）
        src_channel_id = config.comment_sources[chat_id]
        # 使用消息ID作为临时帖子ID
        src_post_id = message.id // 100  # 粗略估计
    else:
        src_channel_id = config.comment_sources[chat_id]
    
    # 处理媒体组
    if message.grouped_id is not None:
        if message.grouped_id not in st.PENDING_COMMENT_GROUPS:
            st.PENDING_COMMENT_GROUPS[message.grouped_id] = []
        st.PENDING_COMMENT_GROUPS[message.grouped_id].append(message)
        
        # 设置定时器
        async def flush_group():
            await asyncio.sleep(2)
            if message.grouped_id in st.PENDING_COMMENT_GROUPS:
                messages = st.PENDING_COMMENT_GROUPS[message.grouped_id]
                if messages:
                    comment = st.PendingComment(
                        message=messages[0],
                        forward=forward,
                        source_channel_id=src_channel_id,
                        source_post_id=src_post_id,
                        grouped_id=message.grouped_id
                    )
                    await _process_comment_batch(event.client, [comment])
        
        asyncio.create_task(flush_group())
        return
    
    # 创建待处理评论
    comment = st.PendingComment(
        message=message,
        forward=forward,
        source_channel_id=src_channel_id,
        source_post_id=src_post_id
    )
    
    # 立即尝试处理
    await _process_comment_batch(event.client, [comment])


async def _periodic_comment_processor(client):
    """定期处理待发送评论"""
    while True:
        await asyncio.sleep(10)
        
        async with st.PENDING_COMMENT_LOCK:
            if not st.PENDING_COMMENTS:
                continue
            
            # 批量处理
            all_comments = []
            for comments in st.PENDING_COMMENTS.values():
                all_comments.extend(comments)
            
            if all_comments:
                logging.info(f"处理 {len(all_comments)} 条待发送评论")
                await _process_comment_batch(client, all_comments)
                st.PENDING_COMMENTS.clear()


# 修改 start_sync 函数，添加评论处理器
async def start_sync():
    clean_session_files()
    await load_async_plugins()
    SESSION = get_SESSION()
    client = TelegramClient(SESSION, CONFIG.login.API_ID, CONFIG.login.API_HASH, sequential_updates=CONFIG.live.sequential_updates)
    if CONFIG.login.user_type == 0:
        if not CONFIG.login.BOT_TOKEN:
            return
        await client.start(bot_token=CONFIG.login.BOT_TOKEN)
    else:
        await client.start()
    config.is_bot = await client.is_bot()
    ALL_EVENTS.update(get_events())
    await config.load_admins(client)
    config.from_to = await config.load_from_to(client, CONFIG.forwards)
    if not config.from_to:
        return
    has_comments = any(f.use_this and f.comments.enabled for f in CONFIG.forwards)
    if has_comments:
        cs, cf = await _setup_comment_listeners(client)
        config.comment_sources = cs
        config.comment_forward_map = cf
        if cs:
            for discussion_id in cs:
                count = await preload_discussion_mappings(client, discussion_id, limit=2000)
                logging.info(f"预加载 {count} 个帖子映射: 讨论组={discussion_id}")
            client.add_event_handler(comment_message_handler, events.NewMessage(chats=list(cs.keys())))
            
            # 启动定期处理器
            asyncio.create_task(_periodic_comment_processor(client))
            logging.info(f"评论监听已启动, 监听讨论组: {list(cs.keys())}")
        else:
            logging.warning("未找到任何可用的讨论组, 评论转发将不会工作")
    for key, val in ALL_EVENTS.items():
        if not CONFIG.live.delete_sync and key == "deleted":
            continue
        client.add_event_handler(*val)
    logging.info("live模式启动完成")
    await client.run_until_disconnected()
