import json
import streamlit as st
from nb.config import CONFIG_FILE_NAME, read_config, write_config
from nb.utils import platform_info
from nb.web_ui.password import check_password
from nb.web_ui.utils import hide_st, switch_theme

CONFIG = read_config()
st.set_page_config(page_title="高级设置", page_icon="🔬")
hide_st(st)
switch_theme(st, CONFIG)

if check_password(st):
    st.warning("此页面面向开发者和高级用户。")
    if st.checkbox("我已了解"):
        with st.expander("版本与平台"):
            st.code(platform_info())
        with st.expander("配置文件"):
            with open(CONFIG_FILE_NAME, "r") as file:
                data = json.loads(file.read())
                dumped = json.dumps(data, indent=3)
            st.download_button("下载配置文件", data=dumped, file_name=CONFIG_FILE_NAME)
            st.json(data)
        with st.expander("Live 模式特殊选项"):
            CONFIG.live.sequential_updates = st.checkbox("强制顺序更新", value=CONFIG.live.sequential_updates)
            CONFIG.live.delete_on_edit = st.text_input("编辑为指定内容时删除消息", value=CONFIG.live.delete_on_edit)
            st.write("当源消息被编辑为指定内容时，将同时删除源和目标中的该消息。")
            if st.checkbox("自定义 Bot 消息"):
                st.info("User 账号的命令以 `.` 开头（如 `.start`），Bot 账号以 `/` 开头（如 `/start`）。")
                CONFIG.bot_messages.start = st.text_area("Bot 回复 /start", value=CONFIG.bot_messages.start)
                CONFIG.bot_messages.bot_help = st.text_area("Bot 回复 /help", value=CONFIG.bot_messages.bot_help)
            if st.button("保存"):
                write_config(CONFIG)
