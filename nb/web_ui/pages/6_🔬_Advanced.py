import json
import streamlit as st
from nb.config import CONFIG_FILE_NAME, read_config, write_config
from nb.utils import platform_info
from nb.web_ui.password import check_password
from nb.web_ui.utils import switch_theme

CONFIG = read_config()

st.set_page_config(page_title="Advanced", page_icon="🔬", layout="wide")
switch_theme(st, CONFIG)

if check_password(st):
    st.title("Advanced Settings")
    
    st.markdown("""
    <div style="background:#fff3cd; color:#856404; padding:15px; border-radius:8px; border:1px solid #ffeeba; margin-bottom:20px;">
        ⚠️ <strong>Warning:</strong> This page allows raw configuration access. Proceed with caution.
    </div>
    """, unsafe_allow_html=True)

    if st.checkbox("I understand the risks"):
        
        with st.expander("System Info"):
            st.code(platform_info())

        with st.expander("Raw Configuration (JSON)"):
            with open(CONFIG_FILE_NAME, "r") as file:
                # 兼容 Pydantic v2 dump 后的 JSON
                data = json.loads(file.read())
                dumped = json.dumps(data, indent=3)
            
            c1, c2 = st.columns([1, 3])
            with c1:
                st.download_button(
                    "📥 Download Config", 
                    data=dumped, 
                    file_name=CONFIG_FILE_NAME,
                    use_container_width=True
                )
            st.json(data)

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

            st.markdown("---")
            st.markdown("**Bot Responses**")
            CONFIG.bot_messages.start = st.text_area(
                "/start Reply", value=CONFIG.bot_messages.start
            )
            CONFIG.bot_messages.bot_help = st.text_area(
                "/help Reply", value=CONFIG.bot_messages.bot_help
            )

            if st.button("💾 Save Advanced Config", type="primary"):
                write_config(CONFIG)
                st.success("Saved!")
