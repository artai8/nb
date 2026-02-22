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
    <div class="glass-card" style="border-left: 4px solid #f59e0b; margin-bottom: 20px;">
        <span style="font-size: 1.2rem; margin-right: 10px;">⚠️</span>
        <strong>Warning:</strong> This page allows raw configuration access. Proceed with caution.
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


