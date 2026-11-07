# nb/web_ui/pages/3_🔄_连接.py
"""Forwarding Channels & Connection Management."""

import streamlit as st
from nb.config import read_config, write_config
from nb.web_ui.password import check_password
from nb.web_ui.utils import render_jelly_hero, switch_theme

CONFIG = read_config()

st.set_page_config(page_title="通道连接", page_icon="🔄", layout="wide")
switch_theme(st, CONFIG)

if check_password(st):
    render_jelly_hero(
        st,
        "通道连接",
        "配置源频道、目标频道、运行模式与评论区同步",
        icon="link",
    )

    with st.form("connection_full_form"):
        col1, col2 = st.columns(2)
        with col1:
            st.markdown('<div class="glass-card"><strong>1. 源与目标通道</strong></div>', unsafe_allow_html=True)
            conn_name = st.text_input("任务名称", value="主频道转发")
            sources_raw = st.text_area("源频道 / 群组 (每行一个 ID 或 @username)", value="-100188291001", height=95)
            dests_raw = st.text_area("目标频道 / 群组 (每行一个 ID 或 @username)", value="-100199384756", height=95)
            show_fwd = st.checkbox("保留转发来源标签", value=False)

        with col2:
            st.markdown('<div class="aqua-card"><strong>2. 运行模式参数</strong></div>', unsafe_allow_html=True)
            mode = st.selectbox("运行模式", options=["live", "past", "schedule"], index=0)
            p1, p2, p3 = st.columns(3)
            with p1:
                past_offset = st.number_input("历史起始 ID", min_value=0, value=0)
            with p2:
                past_end = st.number_input("历史结束 ID (0=不限)", min_value=0, value=0)
            with p3:
                past_delay = st.number_input("发送间隔 (秒)", min_value=0, max_value=100, value=2)

            s1, s2 = st.columns(2)
            with s1:
                sched_time = st.text_input("每日定时时间 (HH:MM)", value="08:00")
                live_del_word = st.text_input("编辑触发删除词", value=".deleteMe")
            with s2:
                sched_limit = st.number_input("每日最大条数 (0=不限)", min_value=0, value=0)
                live_del_sync = st.checkbox("同步撤回删除", value=True)
                live_seq = st.checkbox("顺序更新消息", value=False)

        st.markdown("<br>", unsafe_allow_html=True)
        col3, col4 = st.columns(2)

        with col3:
            st.markdown('<div class="iridescent-card"><strong>3. 评论区同步</strong></div>', unsafe_allow_html=True)
            comm_enabled = st.toggle("启用评论区同步", value=bool(CONFIG.get("comments", {}).get("enabled", False)))
            c_m1, c_m2 = st.columns(2)
            with c_m1:
                comm_src_mode = st.selectbox("源模式", options=["comments", "discussion"], index=0)
                comm_src_grp = st.text_input("源讨论群 ID 或 @username", value="")
            with c_m2:
                comm_dst_mode = st.selectbox("目标模式", options=["comments", "discussion"], index=0)
                comm_dst_grps = st.text_input("目标讨论群列表 (逗号分隔)", value="")

            comm_only_media = st.checkbox("仅同步含媒体评论", value=False)
            comm_inc_text = st.checkbox("包含纯文本评论", value=True)
            comm_skip_bot = st.checkbox("跳过机器人评论", value=False)
            comm_skip_admin = st.checkbox("跳过管理员评论", value=False)
            comm_merge = st.checkbox("合并评论区相册", value=True)
            comm_merge_wait = st.number_input("相册合并等待秒数", min_value=10, max_value=600, value=60)

        with col4:
            st.markdown('<div class="glass-card"><strong>4. 分页媒体抓取</strong></div>', unsafe_allow_html=True)
            bm_enabled = st.toggle("启用分页媒体抓取", value=bool(CONFIG.get("bot_media", {}).get("enabled", True)))
            b1, b2, b3 = st.columns(3)
            with b1:
                bm_poll = st.number_input("轮询间隔(秒)", min_value=0.1, max_value=30.0, value=1.2, step=0.1)
            with b2:
                bm_timeout = st.number_input("等待超时(秒)", min_value=1.0, max_value=120.0, value=12.0, step=1.0)
            with b3:
                bm_max_pages = st.number_input("最大翻页数", min_value=1, max_value=50, value=5)

            bm_page_kw = st.text_input("翻页按钮关键词 (逗号分隔)", value="下一页, Next")
            bm_blacklist = st.text_input("屏蔽链接关键词 (逗号分隔)", value="")
            bm_prefixes = st.text_input("评论触发前缀词 (逗号分隔)", value="评论区回复, 评论区发送")
            bm_suffixes = st.text_input("评论触发后缀词 (逗号分隔)", value="获取资源, 领取, 获取")
            bm_auto_trigger = st.checkbox("自动触发评论关键词", value=True)
            bm_pagination = st.checkbox("自动点击翻页按钮", value=True)
            bm_force_empty = st.checkbox("无资源时仍转发原帖", value=True)

        saved = st.form_submit_button("保存连接配置")
        if saved:
            new_conn = {
                "name": conn_name,
                "mode": mode,
                "sources": [s.strip() for s in sources_raw.splitlines() if s.strip()],
                "destinations": [d.strip() for d in dests_raw.splitlines() if d.strip()],
                "show_forwarded_from": show_fwd,
                "live": {
                    "sequential_updates": live_seq,
                    "delete_sync": live_del_sync,
                    "delete_on_edit": live_del_word,
                },
                "past": {
                    "offset": int(past_offset),
                    "end": int(past_end) if past_end > 0 else None,
                    "delay": int(past_delay),
                },
                "schedule": {
                    "run_time": sched_time,
                    "daily_limit": int(sched_limit),
                },
                "comments": {
                    "enabled": comm_enabled,
                    "source_mode": comm_src_mode,
                    "source_discussion_group": comm_src_grp.strip() or None,
                    "dest_mode": comm_dst_mode,
                    "dest_discussion_groups": [x.strip() for x in comm_dst_grps.split(",") if x.strip()],
                    "only_media": comm_only_media,
                    "include_text_comments": comm_inc_text,
                    "skip_bot_comments": comm_skip_bot,
                    "skip_admin_comments": comm_skip_admin,
                    "merge_comment_media": comm_merge,
                    "merge_wait_seconds": int(comm_merge_wait),
                },
                "bot_media": {
                    "enabled": bm_enabled,
                    "poll_interval": float(bm_poll),
                    "wait_timeout": float(bm_timeout),
                    "max_pages": int(bm_max_pages),
                    "pagination_keywords": [x.strip() for x in bm_page_kw.split(",") if x.strip()],
                    "tme_link_blacklist": [x.strip() for x in bm_blacklist.split(",") if x.strip()],
                    "comment_keyword_prefixes": [x.strip() for x in bm_prefixes.split(",") if x.strip()],
                    "comment_keyword_suffixes": [x.strip() for x in bm_suffixes.split(",") if x.strip()],
                    "auto_comment_trigger_enabled": bm_auto_trigger,
                    "enable_pagination": bm_pagination,
                    "force_forward_on_empty": bm_force_empty,
                },
            }
            conns = CONFIG.get("connections", [])
            conns.append(new_conn)
            CONFIG["connections"] = conns
            CONFIG["comments"] = new_conn["comments"]
            CONFIG["bot_media"] = new_conn["bot_media"]
            write_config(CONFIG)
            st.success("配置已保存")

    st.subheader("已保存连接")
    st.json(CONFIG.get("connections", []))
