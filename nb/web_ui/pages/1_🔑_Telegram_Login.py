# nb/web_ui/pages/1_🔑_Telegram_Login.py —— 修复自动填充

import streamlit as st
import os  # 👈 新增导入

from nb.config import CONFIG, read_config, write_config
from nb.web_ui.password import check_password
from nb.web_ui.utils import hide_st, switch_theme

CONFIG = read_config()

st.set_page_config(
    page_title="Telegram Login",
    page_icon="🔑",
)
hide_st(st)
switch_theme(st, CONFIG)

if check_password(st):

    # ✅ 自动从环境变量读取，若无则使用配置文件中的值
    env_api_id = os.getenv("API_ID", "")
    env_api_hash = os.getenv("API_HASH", "")
    env_session_string = os.getenv("SESSION_STRING", "")
    env_bot_token = os.getenv("BOT_TOKEN", "")

    # 优先使用环境变量，其次用配置中保存的值
    default_api_id = env_api_id or str(CONFIG.login.API_ID)
    default_api_hash = env_api_hash or CONFIG.login.API_HASH
    default_session_string = env_session_string or CONFIG.login.SESSION_STRING
    default_bot_token = env_bot_token or CONFIG.login.BOT_TOKEN

    # 输入框使用默认值（来自 env 或 config）
    input_api_id = st.text_input("API ID", value=default_api_id, type="password")
    input_api_hash = st.text_input("API HASH", value=default_api_hash, type="password")

    st.write("You can get api id and api hash from https://my.telegram.org.")

    user_type = st.radio(
        "Choose account type", ["Bot", "User"], index=CONFIG.login.user_type
    )

    if user_type == "Bot":
        CONFIG.login.user_type = 0
        CONFIG.login.BOT_TOKEN = st.text_input(
            "Enter bot token", value=default_bot_token, type="password"
        )
    else:
        CONFIG.login.user_type = 1
        CONFIG.login.SESSION_STRING = st.text_input(
            "Enter session string", value=default_session_string, type="password"
        )
        with st.expander("How to get session string ?"):
            st.markdown(
                """
            Link to repl: https://replit.com/@artai8/tg-login?v=1

            _Click on the above link and enter api id, api hash, and phone no to generate session string._

            **Note from developer:**

            Due some issues logging in with a user account using a phone no is not supported in this web interface.

            I have built a command-line program named tg-login (https://github.com/artai8/tg-login) that can generate the session string for you.

            You can run tg-login on your computer, or securely in this repl. tg-login is open source, and you can also inspect the bash script running in the repl.

            What is a session string ?
            https://docs.telethon.dev/en/stable/concepts/sessions.html#string-sessions
            """
            )

    # 保存时写入的是用户输入的值（可能覆盖了 env）
    if st.button("Save"):
        try:
            CONFIG.login.API_ID = int(input_api_id)
        except ValueError:
            st.error("API ID must be an integer")
            st.stop()
        CONFIG.login.API_HASH = input_api_hash
        write_config(CONFIG)
        st.success("Configuration saved successfully!")
