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
