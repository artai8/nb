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
    c_head, c_act = st.columns([4, 1])
    with c_head:
        st.title("Connections Manager")
    with c_act:
        st.write("")
        if st.button("➕ New Connection", type="primary", use_container_width=True):
            CONFIG.forwards.append(Forward())
            write_config(CONFIG)
            rerun()

    if not CONFIG.forwards:
        st.warning("No connections found. Create one to get started.")
    else:
        # Custom Tabs
        tab_labels = []
        for i, con in enumerate(CONFIG.forwards):
            status = "🟢" if con.use_this else "⚫"
            name = con.con_name if con.con_name else f"Link #{i+1}"
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
                    st.markdown("#### ⚙️ General Settings")
                    c1, c2, c3 = st.columns([2, 1, 1])
                    with c1:
                        obj.con_name = st.text_input("Connection Name", value=obj.con_name, key=f"n{con}", placeholder="e.g. Channel A -> Group B")
                    with c2:
                        st.write("")
                        st.write("") # Spacer
                        obj.use_this = st.checkbox("Active", value=obj.use_this, key=f"u{con}")
                    with c3:
                        st.write("")
                        st.write("") # Spacer
                        if st.button("🗑️ Delete", key=f"d{con}", type="secondary"):
                            del CONFIG.forwards[i]
                            write_config(CONFIG)
                            rerun()
                
                st.markdown("---")
                
                c_src, c_dst = st.columns(2)
                with c_src:
                    st.markdown("#### 📤 Source")
                    src_val = st.text_input("Source ID / Username", value=str(obj.source), key=f"s{con}")
                    obj.source = _parse_id(src_val)
                    st.caption("Enter ID (e.g. -100xxx) or Username")
                
                with c_dst:
                    st.markdown("#### 📥 Destinations")
                    dst_raw = st.text_area("Destinations (One per line)", value=get_string(obj.dest), key=f"dst{con}", height=100)
                    obj.dest = [_parse_id(x) for x in get_list(dst_raw)]

                # Advanced Settings
                with st.expander("💬 Comments & Discussion Settings", expanded=obj.comments.enabled):
                    st.markdown("##### 🗣️ Comment Mirroring")
                    comments = obj.comments
                    comments.enabled = st.checkbox("Enable Comments Forwarding", value=comments.enabled, key=f"ce{con}")
                    
                    if comments.enabled:
                        cc1, cc2 = st.columns(2)
                        with cc1:
                            st.markdown("**Source Strategy**")
                            comments.source_mode = st.radio("Mode", ["comments (Auto)", "discussion (Manual)"], index=0 if comments.source_mode=="comments" else 1, key=f"csm{con}")
                            if "discussion" in comments.source_mode:
                                dg_in = st.text_input("Source Discussion ID", value=str(comments.source_discussion_group or ""), key=f"csdg{con}")
                                comments.source_discussion_group = _parse_id(dg_in) if dg_in else None
                                comments.source_mode = "discussion"
                            else:
                                comments.source_mode = "comments"

                        with cc2:
                            st.markdown("**Destination Strategy**")
                            comments.dest_mode = st.radio("Mode", ["comments (Auto)", "discussion (Manual)"], index=0 if comments.dest_mode=="comments" else 1, key=f"cdm{con}")
                            if "discussion" in comments.dest_mode:
                                ddg_in = st.text_area("Dest Discussion IDs", value=get_string(comments.dest_discussion_groups), key=f"cddg{con}", height=68)
                                comments.dest_discussion_groups = [_parse_id(x) for x in get_list(ddg_in)]
                                comments.dest_mode = "discussion"
                            else:
                                comments.dest_mode = "comments"
                        
                        st.markdown("**Filters**")
                        cf1, cf2, cf3, cf4 = st.columns(4)
                        with cf1: comments.only_media = st.checkbox("Media Only", value=comments.only_media, key=f"com{con}")
                        with cf2: comments.include_text_comments = st.checkbox("Text OK", value=comments.include_text_comments, key=f"ctok{con}")
                        with cf3: comments.skip_bot_comments = st.checkbox("Skip Bots", value=comments.skip_bot_comments, key=f"csb{con}")
                        with cf4: comments.skip_admin_comments = st.checkbox("Skip Admins", value=comments.skip_admin_comments, key=f"csa{con}")

                        # Mapping logic (simplified for UI, keeping logic)
                        comments.post_mapping_mode = st.selectbox("Post Mapping", ["auto", "manual"], index=0 if comments.post_mapping_mode!="manual" else 1, key=f"cpmm{con}")
                        if comments.post_mapping_mode == "manual":
                            comments.manual_post_mapping_raw = st.text_area("YAML Mapping", value=comments.manual_post_mapping_raw, key=f"cyp{con}")
                            try:
                                mapping = yaml.safe_load(comments.manual_post_mapping_raw)
                                if isinstance(mapping, dict):
                                    comments.manual_post_mapping = {str(k): str(v) for k, v in mapping.items()}
                                else: comments.manual_post_mapping = {}
                            except: comments.manual_post_mapping = {}

                    obj.comments = comments

                with st.expander("🤖 Bot Media Overrides"):
                    enabled_options = ["", "enabled", "disabled"]
                    enabled_labels = {
                        "": "Use Global",
                        "enabled": "Enabled",
                        "disabled": "Disabled",
                    }
                    current_enabled = (
                        "" if obj.bot_media_enabled is None else ("enabled" if obj.bot_media_enabled else "disabled")
                    )
                    selected_enabled = st.selectbox(
                        "Bot media fetch (override)",
                        enabled_options,
                        index=enabled_options.index(current_enabled),
                        format_func=lambda x: enabled_labels.get(x, x),
                        key=f"bme{con}",
                    )
                    if selected_enabled == "enabled":
                        obj.bot_media_enabled = True
                    elif selected_enabled == "disabled":
                        obj.bot_media_enabled = False
                    else:
                        obj.bot_media_enabled = None

                    auto_options = ["", "enabled", "disabled"]
                    auto_labels = {
                        "": "Use Global",
                        "enabled": "Enabled",
                        "disabled": "Disabled",
                    }
                    current_auto = (
                        "" if obj.auto_comment_trigger_enabled is None else ("enabled" if obj.auto_comment_trigger_enabled else "disabled")
                    )
                    selected_auto = st.selectbox(
                        "Auto comment trigger (override)",
                        auto_options,
                        index=auto_options.index(current_auto),
                        format_func=lambda x: auto_labels.get(x, x),
                        key=f"act{con}",
                    )
                    if selected_auto == "enabled":
                        obj.auto_comment_trigger_enabled = True
                    elif selected_auto == "disabled":
                        obj.auto_comment_trigger_enabled = False
                    else:
                        obj.auto_comment_trigger_enabled = None

                    mode_options = ["", "auto", "custom_only", "any"]
                    mode_labels = {
                        "": "Use Global",
                        "auto": "Auto",
                        "custom_only": "Custom Only",
                        "any": "Any Button",
                    }
                    current_mode = obj.bot_media_pagination_mode if obj.bot_media_pagination_mode in mode_options else ""
                    obj.bot_media_pagination_mode = st.selectbox(
                        "Pagination mode (override)",
                        mode_options,
                        index=mode_options.index(current_mode),
                        format_func=lambda x: mode_labels.get(x, x),
                        key=f"bpm{con}",
                    )
                    obj.bot_media_pagination_keywords_raw = st.text_area(
                        "Pagination keywords (override, one per line)",
                        value=obj.bot_media_pagination_keywords_raw,
                        height=80,
                        key=f"bpk{con}",
                    )
                    obj.comment_keyword_prefixes_raw = st.text_area(
                        "Comment keyword prefix (override, one per line)",
                        value=obj.comment_keyword_prefixes_raw,
                        height=80,
                        key=f"bpp{con}",
                    )
                    obj.comment_keyword_suffixes_raw = st.text_area(
                        "Comment keyword suffix (override, one per line)",
                        value=obj.comment_keyword_suffixes_raw,
                        height=80,
                        key=f"bps{con}",
                    )

                with st.expander("🕰️ Past Mode (History) Settings"):
                    hc1, hc2 = st.columns(2)
                    with hc1:
                        off_val = st.text_input("Start Offset ID", value=str(obj.offset), key=f"off{con}")
                        obj.offset = _safe_int(off_val)
                    with hc2:
                        end_val = st.text_input("End ID (Optional)", value=str(obj.end) if obj.end else "", key=f"end{con}")
                        obj.end = _safe_int(end_val, None) if end_val else None

    with st.expander("Live Mode Tweaks"):
        CONFIG.live.sequential_updates = st.checkbox(
            "Enforce sequential updates", value=CONFIG.live.sequential_updates
        )
        
        st.markdown("**Delete-on-Edit Trigger**")
        CONFIG.live.delete_on_edit = st.text_input(
            "Trigger Text", value=CONFIG.live.delete_on_edit
        )
        st.caption("If an edited message matches this text, the message will be deleted.")

        st.markdown("---")
        st.markdown("**Bot Media Fetch**")
        CONFIG.bot_media.enabled = st.checkbox(
            "Enable bot media fetching", value=CONFIG.bot_media.enabled
        )
        CONFIG.bot_media.enable_keyword_trigger = st.checkbox(
            "Enable keyword trigger", value=CONFIG.bot_media.enable_keyword_trigger
        )
        CONFIG.bot_media.enable_pagination = st.checkbox(
            "Enable pagination", value=CONFIG.bot_media.enable_pagination
        )
        pagination_modes = ["auto", "custom_only", "any"]
        pagination_labels = {
            "auto": "Auto",
            "custom_only": "Custom Only",
            "any": "Any Button",
        }
        current_mode = CONFIG.bot_media.pagination_mode if CONFIG.bot_media.pagination_mode in pagination_modes else "auto"
        CONFIG.bot_media.pagination_mode = st.selectbox(
            "Pagination mode",
            pagination_modes,
            index=pagination_modes.index(current_mode),
            format_func=lambda x: pagination_labels.get(x, x),
        )
        CONFIG.bot_media.ignore_filter = st.checkbox(
            "Ignore filter plugin for bot media", value=CONFIG.bot_media.ignore_filter
        )
        CONFIG.bot_media.force_forward_on_empty = st.checkbox(
            "Force forward if plugins drop all", value=CONFIG.bot_media.force_forward_on_empty
        )
        CONFIG.bot_media.poll_interval = st.number_input(
            "Bot poll interval (sec)",
            min_value=0.2,
            max_value=10.0,
            value=float(CONFIG.bot_media.poll_interval),
            step=0.1,
        )
        CONFIG.bot_media.wait_timeout = st.number_input(
            "Bot wait timeout (sec)",
            min_value=2.0,
            max_value=60.0,
            value=float(CONFIG.bot_media.wait_timeout),
            step=1.0,
        )
        CONFIG.bot_media.max_pages = st.number_input(
            "Max pages",
            min_value=0,
            max_value=50,
            value=int(CONFIG.bot_media.max_pages),
            step=1,
        )
        CONFIG.bot_media.recent_limit = st.number_input(
            "Recent messages limit",
            min_value=10,
            max_value=500,
            value=int(CONFIG.bot_media.recent_limit),
            step=10,
        )
        CONFIG.bot_media.pagination_keywords_raw = st.text_area(
            "Pagination keywords (one per line)",
            value=CONFIG.bot_media.pagination_keywords_raw,
            height=80,
        )
        CONFIG.bot_media.comment_keyword_prefixes_raw = st.text_area(
            "Comment keyword prefix (one per line)",
            value=CONFIG.bot_media.comment_keyword_prefixes_raw,
            height=80,
        )
        CONFIG.bot_media.comment_keyword_suffixes_raw = st.text_area(
            "Comment keyword suffix (one per line)",
            value=CONFIG.bot_media.comment_keyword_suffixes_raw,
            height=80,
        )

        st.markdown("---")
        st.markdown("**Bot Responses**")
        CONFIG.bot_messages.start = st.text_area(
            "/start Reply", value=CONFIG.bot_messages.start
        )
        CONFIG.bot_messages.bot_help = st.text_area(
            "/help Reply", value=CONFIG.bot_messages.bot_help
        )

    st.markdown("---")
    if st.button("💾 Save All Changes", type="primary", use_container_width=True):
        write_config(CONFIG)
        st.toast("Configuration saved successfully!", icon="✅")
        time.sleep(1)
        rerun()
