import asyncio
import logging
from typing import Dict, List, Optional, Set
from telethon import TelegramClient, events
from telethon.tl.custom.message import Message
from nb import config, const
from nb import storage as st
from nb.bot import get_events
from nb.config import CONFIG, get_SESSION
from nb.plugins import apply_plugins, apply_plugins_to_group, load_async_plugins
from nb.utils import (
    clean_session_files, extract_msg_id, send_message,
    _get_reply_to_msg_id, _get_reply_to_top_id, _extract_channel_post,
    get_discussion_message, get_discussion_group_id,
    walk_to_header, preload_discussion_mappings,
    COMMENT_MAX_RETRIES, COMMENT_RETRY_BASE_DELAY,
)

COMMENT_GROUPED_CACHE: Dict[int, Dict[int, List[Message]]] = {}
COMMENT_GROUPED_TIMERS: Dict[int, asyncio.TimerHandle] = {}
COMMENT_GROUPED_TIMEOUT = 3.0
PROCESSED_COMMENTS: Set[int] = set()


async def _resolve_channel_post_id(client, chat_id, message) -> Optional[int]:
    """解析评论所属的主帖子ID"""
    # 方法1：通过 reply_to_top_id
    top_id = _get_reply_to_top_id(message)
    if top_id is not None:
        cp = st.get_channel_post_id(chat_id, top_id)
        if cp:
            return cp
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
        try:
            reply_msg = await client.get_messages(chat_id, ids=reply_msg_id)
            if reply_msg:
                fwd = _extract_channel_post(reply_msg)
                if fwd:
                    st.add_discussion_mapping(chat_id, reply_msg_id, fwd)
                    return fwd
        except Exception:
            pass
    
    # 方法3：向上遍历
    header_id, cp = await walk_to_header(client, chat_id, message.id)
    if cp:
        return cp
    
    return None


async def _get_dest_targets(client, src_channel_id, src_post_id, forward) -> Dict[int, Optional[int]]:
    """获取评论的目标位置"""
    dest_targets = {}
    
    for dest_ch in forward.dest:
        dest_resolved = dest_ch
        if not isinstance(dest_resolved, int):
            try:
                dest_resolved = await config.get_id(client, dest_ch)
            except Exception:
                continue
        
        dest_post_id = st.get_dest_post_id(src_channel_id, src_post_id, dest_resolved)
        if dest_post_id is None:
            logging.debug(f"未找到映射: {src_channel_id}/{src_post_id} -> {dest_resolved}")
            continue
        
        if forward.comments.dest_mode == "comments":
            disc_msg = await get_discussion_message(client, dest_resolved, dest_post_id)
            if disc_msg:
                dest_targets[disc_msg.chat_id] = disc_msg.id
            else:
                dest_targets[dest_resolved] = dest_post_id
        elif forward.comments.dest_mode == "discussion":
            for dg in forward.comments.dest_discussion_groups:
                dg_id = dg
                if not isinstance(dg_id, int):
                    try:
                        dg_id = await config.get_id(client, dg)
                    except Exception:
                        continue
                dest_targets[dg_id] = None
    
    return dest_targets


async def _send_single_comment(client, message, dest_targets: Dict[int, Optional[int]], chat_id: int):
    """发送单条评论"""
    if message.id in PROCESSED_COMMENTS:
        return
    
    tm = await apply_plugins(message)
    if not tm:
        return
    
    try:
        for dest_chat_id, dest_reply_to in dest_targets.items():
            try:
                fwded = await send_message(dest_chat_id, tm, comment_to_post=dest_reply_to)
                if fwded:
                    st.add_comment_mapping(chat_id, message.id, dest_chat_id, extract_msg_id(fwded))
                    PROCESSED_COMMENTS.add(message.id)
                    logging.info(f"评论发送成功: {chat_id}/{message.id} -> {dest_chat_id}")
            except Exception as e:
                logging.error(f"评论发送失败: {e}")
    finally:
        tm.clear()


async def _send_grouped_comments(client, messages: List[Message], dest_targets: Dict[int, Optional[int]], chat_id: int):
    """发送评论媒体组"""
    if not messages:
        return
    
    first_id = messages[0].id
    if first_id in PROCESSED_COMMENTS:
        return
    
    tms = await apply_plugins_to_group(messages)
    if not tms or tms[0] is None:
        return
    
    try:
        for dest_chat_id, dest_reply_to in dest_targets.items():
            try:
                fwded = await send_message(
                    dest_chat_id, tms[0],
                    grouped_messages=[tm.message for tm in tms],
                    grouped_tms=tms,
                    comment_to_post=dest_reply_to
                )
                if fwded:
                    st.add_comment_mapping(chat_id, first_id, dest_chat_id, extract_msg_id(fwded))
                    PROCESSED_COMMENTS.add(first_id)
                    logging.info(f"评论媒体组发送成功: {chat_id}/{first_id} -> {dest_chat_id}")
            except Exception as e:
                logging.error(f"评论媒体组发送失败: {e}")
    finally:
        for tm in tms:
            tm.clear()


async def _flush_comment_group(grouped_id):
    """处理评论媒体组"""
    if grouped_id not in COMMENT_GROUPED_CACHE:
        return
    
    try:
        chat_messages_map = COMMENT_GROUPED_CACHE[grouped_id]
        for chat_id, messages in chat_messages_map.items():
            if chat_id not in config.comment_sources:
                continue
            
            forward = config.comment_forward_map.get(chat_id)
            if forward is None or not forward.comments.enabled:
                continue
            
            # 解析所属帖子
            src_post_id = await _resolve_channel_post_id(messages[0].client, chat_id, messages[0])
            if src_post_id is None:
                logging.warning(f"媒体组无法解析帖子: grouped_id={grouped_id}")
                continue
            
            src_channel_id = config.comment_sources[chat_id]
            dest_targets = await _get_dest_targets(messages[0].client, src_channel_id, src_post_id, forward)
            
            if dest_targets:
                await _send_grouped_comments(messages[0].client, messages, dest_targets, chat_id)
            else:
                logging.warning(f"媒体组无目标: grouped_id={grouped_id}, src_post={src_post_id}")
    except Exception as e:
        logging.exception(f"处理评论媒体组失败: {e}")
    finally:
        COMMENT_GROUPED_CACHE.pop(grouped_id, None)
        COMMENT_GROUPED_TIMERS.pop(grouped_id, None)


def _add_comment_to_group_cache(chat_id, grouped_id, message):
    """添加评论到媒体组缓存"""
    if grouped_id not in COMMENT_GROUPED_CACHE:
        COMMENT_GROUPED_CACHE[grouped_id] = {}
    if chat_id not in COMMENT_GROUPED_CACHE[grouped_id]:
        COMMENT_GROUPED_CACHE[grouped_id][chat_id] = []
    COMMENT_GROUPED_CACHE[grouped_id][chat_id].append(message)
    
    if grouped_id in COMMENT_GROUPED_TIMERS:
        COMMENT_GROUPED_TIMERS[grouped_id].cancel()
    
    loop = asyncio.get_running_loop()
    COMMENT_GROUPED_TIMERS[grouped_id] = loop.call_later(
        COMMENT_GROUPED_TIMEOUT,
        lambda gid=grouped_id: asyncio.ensure_future(_flush_comment_group(gid)),
    )


async def _send_grouped_messages(grouped_id):
    """发送主帖子媒体组"""
    if grouped_id not in st.GROUPED_CACHE:
        return
    chat_messages_map = st.GROUPED_CACHE[grouped_id]
    for chat_id, messages in chat_messages_map.items():
        if chat_id not in config.from_to:
            continue
        dest = config.from_to.get(chat_id)
        tms = await apply_plugins_to_group(messages)
        if not tms:
            continue
        for d in dest:
            try:
                fwded_msgs = await send_message(d, tms[0], grouped_messages=[tm.message for tm in tms], grouped_tms=tms)
                for i, msg in enumerate(messages):
                    uid = st.EventUid(st.DummyEvent(chat_id, msg.id))
                    if uid not in st.stored:
                        st.stored[uid] = {}
                    if isinstance(fwded_msgs, list) and i < len(fwded_msgs):
                        st.stored[uid][d] = fwded_msgs[i]
                    elif not isinstance(fwded_msgs, list):
                        st.stored[uid][d] = fwded_msgs
                    if i == 0:
                        fid = extract_msg_id(fwded_msgs[0] if isinstance(fwded_msgs, list) else fwded_msgs)
                        if fid is not None:
                            st.add_post_mapping(chat_id, msg.id, d, fid)
            except Exception as e:
                logging.critical(f"live组播失败: {e}")
    st.GROUPED_CACHE.pop(grouped_id, None)
    st.GROUPED_TIMERS.pop(grouped_id, None)
    st.GROUPED_MAPPING.pop(grouped_id, None)


async def new_message_handler(event):
    chat_id = event.chat_id
    if chat_id not in config.from_to:
        return
    message = event.message
    if message.grouped_id is not None:
        st.add_to_group_cache(chat_id, message.grouped_id, message)
        return
    uid = st.EventUid(event)
    if len(st.stored) > const.KEEP_LAST_MANY:
        del st.stored[next(iter(st.stored))]
    dest = config.from_to.get(chat_id)
    tm = await apply_plugins(message)
    if not tm:
        return
    st.stored[uid] = {}
    for d in dest:
        reply_to_id = None
        if event.is_reply:
            rmid = _get_reply_to_msg_id(event.message)
            if rmid is not None:
                r_uid = st.EventUid(st.DummyEvent(chat_id, rmid))
                if r_uid in st.stored:
                    reply_to_id = extract_msg_id(st.stored[r_uid].get(d))
        tm.reply_to = reply_to_id
        try:
            fwded = await send_message(d, tm)
            if fwded is not None:
                st.stored[uid][d] = fwded
                fid = extract_msg_id(fwded)
                if fid is not None:
                    st.add_post_mapping(chat_id, message.id, d, fid)
        except Exception as e:
            logging.error(f"live发送失败: {e}")
    tm.clear()


async def comment_message_handler(event):
    """评论消息处理器"""
    chat_id = event.chat_id
    message = event.message
    
    if chat_id not in config.comment_sources:
        return
    
    forward = config.comment_forward_map.get(chat_id)
    if not forward or not forward.comments.enabled:
        return
    
    # 检查帖子头消息
    cp = _extract_channel_post(message)
    if cp:
        st.add_discussion_mapping(chat_id, message.id, cp)
        logging.debug(f"发现帖子头: {chat_id}/{message.id} -> {cp}")
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
        except Exception:
            pass
    
    # 处理媒体组
    if message.grouped_id is not None:
        _add_comment_to_group_cache(chat_id, message.grouped_id, message)
        return
    
    # 解析所属帖子
    src_post_id = await _resolve_channel_post_id(event.client, chat_id, message)
    if src_post_id is None:
        logging.warning(f"无法解析评论帖子: {chat_id}/{message.id}")
        return
    
    src_channel_id = config.comment_sources[chat_id]
    dest_targets = await _get_dest_targets(event.client, src_channel_id, src_post_id, forward)
    
    if not dest_targets:
        logging.warning(f"评论无目标: {chat_id}/{message.id}, src_post={src_post_id}")
        return
    
    await _send_single_comment(event.client, message, dest_targets, chat_id)


async def edited_message_handler(event):
    chat_id = event.chat_id
    if chat_id not in config.from_to:
        return
    uid = st.EventUid(event)
    if uid not in st.stored:
        return
    if CONFIG.live.delete_on_edit and event.message.text == CONFIG.live.delete_on_edit:
        for d in config.from_to.get(chat_id, []):
            mid = extract_msg_id(st.stored[uid].get(d))
            if mid is not None:
                try:
                    await event.client.delete_messages(d, mid)
                except Exception:
                    pass
        try:
            await event.message.delete()
        except Exception:
            pass
        del st.stored[uid]
        return
    tm = await apply_plugins(event.message)
    if not tm:
        return
    for d in config.from_to.get(chat_id, []):
        mid = extract_msg_id(st.stored[uid].get(d))
        if mid is not None:
            try:
                await event.client.edit_message(d, mid, tm.text)
            except Exception:
                pass
    tm.clear()


async def deleted_message_handler(event):
    for did in event.deleted_ids:
        for chat_id in list(config.from_to.keys()):
            uid = st.EventUid(st.DummyEvent(chat_id, did))
            if uid not in st.stored:
                continue
            for d, fwded in st.stored[uid].items():
                mid = extract_msg_id(fwded)
                if mid is not None:
                    try:
                        await event.client.delete_messages(d, mid)
                    except Exception:
                        pass
            del st.stored[uid]


ALL_EVENTS = {
    "new": (new_message_handler, events.NewMessage()),
    "edited": (edited_message_handler, events.MessageEdited()),
    "deleted": (deleted_message_handler, events.MessageDeleted()),
}


async def _setup_comment_listeners(client):
    comment_sources = {}
    comment_forward_map = {}
    for forward in CONFIG.forwards:
        if not forward.use_this or not forward.comments.enabled:
            continue
        src = forward.source
        if not isinstance(src, int):
            try:
                src = await config.get_id(client, forward.source)
            except Exception:
                continue
        if forward.comments.source_mode == "discussion":
            dg = forward.comments.source_discussion_group
            if dg is None:
                continue
            if not isinstance(dg, int):
                try:
                    dg = await config.get_id(client, dg)
                except Exception:
                    continue
            comment_sources[dg] = src
            comment_forward_map[dg] = forward
            logging.info(f"评论监听(手动): 讨论组={dg} -> 源频道={src}")
        else:
            dg_id = await get_discussion_group_id(client, src)
            if dg_id is None:
                logging.warning(f"无法获取频道 {src} 的讨论组")
                continue
            comment_sources[dg_id] = src
            comment_forward_map[dg_id] = forward
            logging.info(f"评论监听(自动): 讨论组={dg_id} -> 源频道={src}")
    return comment_sources, comment_forward_map


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
            logging.info(f"评论监听已启动, 监听讨论组: {list(cs.keys())}")
        else:
            logging.warning("未找到任何可用的讨论组, 评论转发将不会工作")
    for key, val in ALL_EVENTS.items():
        if not CONFIG.live.delete_sync and key == "deleted":
            continue
        client.add_event_handler(*val)
    logging.info("live模式启动完成")
    await client.run_until_disconnected()
