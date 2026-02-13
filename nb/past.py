async def _forward_comments_for_post(client, src_channel_id, src_post_id, dest_list, forward):
    """转发指定帖子的所有评论（确保映射已存在）"""
    cfg = forward.comments
    
    # 短暂延迟，确保主帖子处理完成
    await asyncio.sleep(2)
    
    # 获取所有评论
    comments = await _get_all_comments(client, src_channel_id, src_post_id)
    if not comments:
        logging.info(f"帖子 {src_post_id} 没有评论")
        return
    
    logging.info(f"帖子 {src_post_id} 有 {len(comments)} 条评论")
    
    # 过滤评论
    filtered = []
    for c in comments:
        if isinstance(c, MessageService):
            continue
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
            except:
                pass
        filtered.append(c)
    
    if not filtered:
        return
    
    logging.info(f"过滤后剩余 {len(filtered)} 条评论")
    
    # 确保所有目标都已建立映射
    dest_targets = {}
    for dest_ch in dest_list:
        dest_resolved = dest_ch
        if not isinstance(dest_resolved, int):
            try:
                dest_resolved = await config.get_id(client, dest_ch)
            except:
                continue
        
        # 必须已有映射
        dest_post_id = st.get_dest_post_id(src_channel_id, src_post_id, dest_resolved)
        if dest_post_id is None:
            logging.error(f"主帖子映射不存在: {src_channel_id}/{src_post_id} -> {dest_resolved}")
            continue
        
        if cfg.dest_mode == "comments":
            disc_msg = await get_discussion_message(client, dest_resolved, dest_post_id)
            if disc_msg:
                dest_targets[disc_msg.chat_id] = disc_msg.id
                logging.info(f"目标讨论组: {disc_msg.chat_id}/{disc_msg.id}")
            else:
                dest_targets[dest_resolved] = dest_post_id
                logging.info(f"目标频道: {dest_resolved}/{dest_post_id}")
        else:
            for dg in cfg.dest_discussion_groups:
                try:
                    dg_id = await config.get_id(client, dg) if not isinstance(dg, int) else dg
                    dest_targets[dg_id] = None
                except:
                    continue
    
    if not dest_targets:
        logging.error(f"没有有效的目标，跳过评论转发")
        return
    
    # 分组处理
    units = _group_comments(filtered)
    logging.info(f"分为 {len(units)} 个单元（单条或媒体组）")
    
    for i, unit_msgs in enumerate(units):
        logging.info(f"处理单元 {i+1}/{len(units)}，包含 {len(unit_msgs)} 条消息")
        
        if len(unit_msgs) > 1:
            # 媒体组
            tms = await apply_plugins_to_group(unit_msgs)
            if not tms or not tms[0]:
                continue
            
            for dest_chat_id, dest_reply_to in dest_targets.items():
                try:
                    logging.info(f"发送媒体组到 {dest_chat_id}")
                    fwded = await send_message(
                        dest_chat_id, tms[0],
                        grouped_messages=[tm.message for tm in tms],
                        grouped_tms=tms,
                        comment_to_post=dest_reply_to
                    )
                    if fwded:
                        st.add_comment_mapping(src_channel_id, unit_msgs[0].id, dest_chat_id, extract_msg_id(fwded))
                        logging.info(f"✅ 媒体组发送成功")
                except Exception as e:
                    logging.error(f"❌ 媒体组发送失败: {e}")
            
            for tm in tms:
                tm.clear()
        else:
            # 单条
            comment = unit_msgs[0]
            tm = await apply_plugins(comment)
            if not tm:
                continue
            
            for dest_chat_id, dest_reply_to in dest_targets.items():
                try:
                    logging.info(f"发送单条评论到 {dest_chat_id}")
                    fwded = await send_message(dest_chat_id, tm, comment_to_post=dest_reply_to)
                    if fwded:
                        st.add_comment_mapping(src_channel_id, comment.id, dest_chat_id, extract_msg_id(fwded))
                        logging.info(f"✅ 评论发送成功")
                except Exception as e:
                    logging.error(f"❌ 评论发送失败: {e}")
            
            tm.clear()
        
        await asyncio.sleep(random.randint(2, 5))
