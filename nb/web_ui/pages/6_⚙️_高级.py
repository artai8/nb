import json
import streamlit as st
from nb.config import CONFIG_FILE_NAME, read_config, write_config
from nb.utils import platform_info
from nb.web_ui.password import check_password
from nb.web_ui.utils import switch_theme

CONFIG = read_config()

st.set_page_config(page_title="高级设置", page_icon="🔬", layout="wide")
switch_theme(st, CONFIG)

if check_password(st):
    st.title("高级设置")
    
    st.markdown("""
    <div class="glass-card" style="border-left: 4px solid #f59e0b; margin-bottom: 20px;">
        <span style="font-size: 1.2rem; margin-right: 10px;">⚠️</span>
        <strong>警告：</strong> 此页面允许直接访问原始配置。请谨慎操作。
    </div>
    """, unsafe_allow_html=True)

    if st.checkbox("我了解风险"):
        
        with st.expander("系统信息"):
            st.code(platform_info())

        with st.expander("原始配置 (JSON)"):
            with open(CONFIG_FILE_NAME, "r") as file:
                # 兼容 Pydantic v2 dump 后的 JSON
                data = json.loads(file.read())
                dumped = json.dumps(data, indent=3)
            
            c1, c2 = st.columns([1, 3])
            with c1:
                st.download_button(
                    "📥 下载配置", 
                    data=dumped, 
                    file_name=CONFIG_FILE_NAME,
                    use_container_width=True
                )
            st.json(data)
