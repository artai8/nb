# nb/web_ui/pages/3_🔗_Connections.py

import time
import streamlit as st
import yaml

from nb.config import CONFIG, Forward, read_config, write_config
from nb.web_ui.password import check_password
from nb.web_ui.utils import get_list, get_string, switch_theme

CONFIG = read_config()

st.set_page_config(
    page_title="Connections",
    page_icon="🔗",
    layout="wide"
)
switch_theme(st, CONFIG)

def rerun():
    if hasattr(st, 'rerun'):
        st.rerun()
    elif hasattr(st, 'experimental_rerun'):
        st.experimental_rerun()
    else:
        raise st.script_runner.StopException

def _parse_id(value: str):
    value = value.strip()
    try:
        return int(value)
    except ValueError:
        return value

def _safe_int(value, default=0):
    if value is None:
        return default
    try:
        return int(value)
    except (ValueError, TypeError):
        return default

if check_password(st):
    
    # Header
    c_head, c_act = st.columns([6, 1])
    with c_head:
        st.write("") # 移除 "连接管理器" 标题，保留占位
    with c_act:
        st.write("")
        if st.button("➕ 新连接", type="primary", use_container_width=True):
            CONFIG.forwards.append(Forward())
            write_config(CONFIG)
            rerun()

    if not CONFIG.forwards:
        st.warning("暂无连接。请创建一个新连接以开始使用。")
    else:
        # Custom Tabs
        tab_labels = []
        for i, con in enumerate(CONFIG.forwards):
            status = "🟢" if con.use_this else "⚫"
            name = con.con_name if con.con_name else f"连接 #{i+1}"
            if con.comments.enabled:
                name += " 💬"
            tab_labels.append(f"{status} {name}")
            
        tabs = st.tabs(tab_labels)

        for i, tab in enumerate(tabs):
            with tab:
                con = i + 1
                obj = CONFIG.forwards[i]
                
                # --- 卡片容器 ---
                with st.container():
                    st.markdown("#### ⚙️ 常规设置")
                    c1, c2 = st.columns([3, 1])
                    with c1:
                        obj.con_name = st.text_input("连接名称", value=obj.con_name, key=f"n{con}", placeholder="例如：频道 A -> 群组 B")
                    with c2:
                        st.write("")
                        st.write("") # Spacer
                        obj.use_this = st.checkbox("启用", value=obj.use_this, key=f"u{con}")
                
                st.markdown("---")
                
                with st.expander("📤 来源与目的地", expanded=False):
                    c_src, c_dst = st.columns(2)
                    with c_src:
                        st.markdown("#### 来源")
                        src_val = st.text_input("来源 ID / 用户名", value=str(obj.source), key=f"s{con}")
                        obj.source = _parse_id(src_val)
                        st.caption("输入 ID (如 -100xxx) 或 用户名")
                    
                    with c_dst:
                        st.markdown("#### 目的地")
                        dst_raw = st.text_area("目的地 (每行一个)", value=get_string(obj.dest), key=f"dst{con}", height=100)
                        obj.dest = [_parse_id(x) for x in get_list(dst_raw)]

                # Advanced Settings
                with st.expander("💬 评论与讨论组设置", expanded=obj.comments.enabled):
                    st.markdown("##### 🗣️ 评论镜像")
                    comments = obj.comments
                    comments.enabled = st.checkbox("启用评论转发", value=comments.enabled, key=f"ce{con}")
                    
                    if comments.enabled:
                        cc1, cc2 = st.columns(2)
                        with cc1:
                            st.markdown("**来源策略**")
                            comments.source_mode = st.radio("模式", ["comments (自动)", "discussion (手动)"], index=0 if comments.source_mode=="comments" else 1, key=f"csm{con}")
                            if "discussion" in comments.source_mode:
                                dg_in = st.text_input("来源讨论组 ID", value=str(comments.source_discussion_group or ""), key=f"csdg{con}")
                                comments.source_discussion_group = _parse_id(dg_in) if dg_in else None
                                comments.source_mode = "discussion"
                            else:
                                comments.source_mode = "comments"

                        with cc2:
                            st.markdown("**目的地策略**")
                            comments.dest_mode = st.radio("模式", ["comments (自动)", "discussion (手动)"], index=0 if comments.dest_mode=="comments" else 1, key=f"cdm{con}")
                            if "discussion" in comments.dest_mode:
                                ddg_in = st.text_area("目的地讨论组 ID (每行一个)", value=get_string(comments.dest_discussion_groups), key=f"cddg{con}", height=68)
                                comments.dest_discussion_groups = [_parse_id(x) for x in get_list(ddg_in)]
                                comments.dest_mode = "discussion"
                            else:
                                comments.dest_mode = "comments"
                        
                        st.markdown("**过滤器**")
                        cf1, cf2, cf3, cf4 = st.columns(4)
                        with cf1: comments.only_media = st.checkbox("仅媒体", value=comments.only_media, key=f"com{con}")
                        with cf2: comments.include_text_comments = st.checkbox("允许文本", value=comments.include_text_comments, key=f"ctok{con}")
                        with cf3: comments.skip_bot_comments = st.checkbox("跳过机器人", value=comments.skip_bot_comments, key=f"csb{con}")
                        with cf4: comments.skip_admin_comments = st.checkbox("跳过管理员", value=comments.skip_admin_comments, key=f"csa{con}")

                        # Mapping logic (simplified for UI, keeping logic)
                        comments.post_mapping_mode = st.selectbox("帖子映射模式", ["auto", "manual"], index=0 if comments.post_mapping_mode!="manual" else 1, key=f"cpmm{con}")
                        if comments.post_mapping_mode == "manual":
                            comments.manual_post_mapping_raw = st.text_area("YAML 映射配置", value=comments.manual_post_mapping_raw, key=f"cyp{con}")
                            try:
                                mapping = yaml.safe_load(comments.manual_post_mapping_raw)
                                if isinstance(mapping, dict):
                                    comments.manual_post_mapping = {str(k): str(v) for k, v in mapping.items()}
                                else: comments.manual_post_mapping = {}
                            except: comments.manual_post_mapping = {}

                    obj.comments = comments

                with st.expander("🤖 机器人媒体覆盖设置", expanded=bool(obj.bot_media_enabled)):
                    enabled_override = st.checkbox(
                        "启用此连接的机器人媒体抓取",
                        value=obj.bot_media_enabled is True,
                        key=f"bme{con}",
                    )
                    obj.bot_media_enabled = True if enabled_override else False

                    if enabled_override:
                        ckw1, ckw2 = st.columns(2)
                        with ckw1:
                            keyword_trigger_enabled = st.checkbox(
                                "关键词触发",
                                value=obj.bot_media_keyword_trigger_enabled is not False,
                                key=f"bmk{con}",
                            )
                            obj.bot_media_keyword_trigger_enabled = True if keyword_trigger_enabled else False
                        with ckw2:
                            auto_comment_enabled = st.checkbox(
                                "自动评论触发",
                                value=obj.auto_comment_trigger_enabled is not False,
                                key=f"act{con}",
                            )
                            obj.auto_comment_trigger_enabled = True if auto_comment_enabled else False

                        mode_options = ["auto", "any"]
                        mode_labels = {
                            "auto": "自动 (Auto)",
                            "any": "任意按钮 (Any Button)",
                        }
                        current_mode = obj.bot_media_pagination_mode if obj.bot_media_pagination_mode in mode_options else "auto"
                        obj.bot_media_pagination_mode = st.selectbox(
                            "翻页模式",
                            mode_options,
                            index=mode_options.index(current_mode),
                            format_func=lambda x: mode_labels.get(x, x),
                            key=f"bpm{con}",
                        )
                        obj.bot_media_pagination_keywords_raw = st.text_area(
                            "翻页关键词 (每行一个)",
                            value=obj.bot_media_pagination_keywords_raw,
                            height=80,
                            key=f"bpk{con}",
                        )
                        obj.bot_media_pagination_ignore_keywords_raw = st.text_area(
                            "翻页忽略关键词 (每行一个)",
                            value=obj.bot_media_pagination_ignore_keywords_raw,
                            height=80,
                            key=f"bpki{con}",
                        )
                        obj.bot_media_tme_link_blacklist_raw = st.text_area(
                            "t.me 链接黑名单 (每行一个)",
                            value=obj.bot_media_tme_link_blacklist_raw,
                            height=80,
                            key=f"bmtl{con}",
                        )
                        obj.comment_keyword_prefixes_raw = st.text_area(
                            "评论关键词前缀 (每行一个)",
                            value=obj.comment_keyword_prefixes_raw,
                            height=80,
                            key=f"bpp{con}",
                        )
                        obj.comment_keyword_suffixes_raw = st.text_area(
                            "评论关键词后缀 (每行一个)",
                            value=obj.comment_keyword_suffixes_raw,
                            height=80,
                            key=f"bps{con}",
                        )

                with st.expander("🕰️ 历史模式设置 (Past Mode)"):
                    hc1, hc2 = st.columns(2)
                    with hc1:
                        off_val = st.text_input("起始消息 ID", value=str(obj.offset), key=f"off{con}")
                        obj.offset = _safe_int(off_val)
                    with hc2:
                        end_val = st.text_input("结束消息 ID (可选)", value=str(obj.end) if obj.end else "", key=f"end{con}")
                        obj.end = _safe_int(end_val, None) if end_val else None

                st.markdown("---")
                # 底部按钮区
                b_col1, b_col2 = st.columns(2)
                with b_col1:
                    if st.button("💾 保存更改", key=f"save_btn_{con}", type="primary", use_container_width=True):
                        write_config(CONFIG)
                        st.toast("配置已保存！", icon="✅")
                        time.sleep(1)
                        rerun()
                with b_col2:
                    if st.button("🗑️ 删除连接", key=f"del_btn_{con}", type="secondary", use_container_width=True):
                        del CONFIG.forwards[i]
                        write_config(CONFIG)
                        rerun()
