# nb/web_ui/pages/1_🔑_Telegram_Login.py

import streamlit as st
import os

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

    # ★ 从环境变量读取（优先级最高）
    env_api_id = os.getenv("API_ID", "")
    env_api_hash = os.getenv("API_HASH", "")
    env_session_string = os.getenv("SESSION_STRING", "")
    env_bot_token = os.getenv("BOT_TOKEN", "")

    # ★ 显示环境变量状态提示
    env_vars_found = []
    if env_api_id:
        env_vars_found.append("API_ID")
    if env_api_hash:
        env_vars_found.append("API_HASH")
    if env_session_string:
        env_vars_found.append("SESSION_STRING")
    if env_bot_token:
        env_vars_found.append("BOT_TOKEN")

    if env_vars_found:
        st.info(
            f"🔍 检测到环境变量: {', '.join(env_vars_found)}\n\n"
            "环境变量的值会作为默认值自动填入。保存后会写入配置文件。"
        )

    # ★ 自动推断：如果环境变量有 SESSION_STRING 但没有 BOT_TOKEN → 应该是 User
    #   如果环境变量有 BOT_TOKEN 但没有 SESSION_STRING → 应该是 Bot
    auto_user_type = CONFIG.login.user_type
    if env_session_string and not env_bot_token:
        auto_user_type = 1  # User
    elif env_bot_token and not env_session_string:
        auto_user_type = 0  # Bot

    # 优先使用环境变量，其次用配置中保存的值
    default_api_id = env_api_id or str(CONFIG.login.API_ID)
    default_api_hash = env_api_hash or CONFIG.login.API_HASH
    default_session_string = env_session_string or CONFIG.login.SESSION_STRING
    default_bot_token = env_bot_token or CONFIG.login.BOT_TOKEN

    # 输入框
    input_api_id = st.text_input("API ID", value=default_api_id, type="password")
    input_api_hash = st.text_input("API HASH", value=default_api_hash, type="password")

    st.write("You can get api id and api hash from https://my.telegram.org.")

    user_type = st.radio(
        "Choose account type", ["Bot", "User"], index=auto_user_type
    )

    # ★ 根据选择显示对应的输入框，并保存到临时变量
    input_bot_token = ""
    input_session_string = ""

    if user_type == "Bot":
        selected_user_type = 0
        input_bot_token = st.text_input(
            "Enter bot token", value=default_bot_token, type="password"
        )
        if not input_bot_token:
            st.warning("⚠️ Bot Token 为空")
    else:
        selected_user_type = 1
        input_session_string = st.text_input(
            "Enter session string", value=default_session_string, type="password"
        )

        # ★ 检测误填 Bot Token
        if input_session_string:
            ss = input_session_string.strip()
            if ":" in ss and len(ss) < 100:
                parts = ss.split(":", 1)
                if parts[0].isdigit():
                    st.error(
                        "🚫 **这看起来是 Bot Token，不是 Session String！**\n\n"
                        "- Bot Token 格式: `123456789:ABCdefGHIjkl...` (短，含冒号)\n"
                        "- Session String 格式: `1BQANOTEuMT...` (长，200+ 字符)\n\n"
                        "如果你要用 Bot 账号，请切换为 Bot 类型。\n"
                        "如果你要用 User 账号，请使用 tg-login 生成正确的 Session String。"
                    )

        if not input_session_string:
            st.warning("⚠️ Session String 为空")

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

    # ★★★ 保存按钮：统一保存所有字段 ★★★
    if st.button("Save"):
        # 验证 API_ID
        try:
            save_api_id = int(input_api_id)
        except ValueError:
            st.error("API ID must be an integer")
            st.stop()

        # 写入所有字段
        CONFIG.login.API_ID = save_api_id
        CONFIG.login.API_HASH = input_api_hash.strip()
        CONFIG.login.user_type = selected_user_type

        if selected_user_type == 0:
            # Bot 模式
            CONFIG.login.BOT_TOKEN = input_bot_token.strip()
            # ★ 清空 Session String，避免冲突
            CONFIG.login.SESSION_STRING = ""
            login_desc = "Bot"
        else:
            # User 模式
            CONFIG.login.SESSION_STRING = input_session_string.strip()
            # ★ 清空 Bot Token，避免冲突
            CONFIG.login.BOT_TOKEN = ""
            login_desc = "User"

        write_config(CONFIG)

        st.success(
            f"✅ 配置已保存！\n\n"
            f"- 账号类型: **{login_desc}**\n"
            f"- API_ID: `{save_api_id}`\n"
            f"- API_HASH: `{CONFIG.login.API_HASH[:8]}...`\n"
            f"- {'Bot Token' if selected_user_type == 0 else 'Session String'}: "
            f"{'已设置 ✅' if (input_bot_token if selected_user_type == 0 else input_session_string) else '未设置 ❌'}"
        )

    # ★ 显示当前配置状态
    st.markdown("---")
    st.markdown("##### 当前保存的配置状态")

    config_type = "Bot" if CONFIG.login.user_type == 0 else "User"
    has_bot_token = "✅" if CONFIG.login.BOT_TOKEN else "❌"
    has_session = "✅" if CONFIG.login.SESSION_STRING else "❌"

    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("账号类型", config_type)
    with col2:
        st.metric("Bot Token", has_bot_token)
    with col3:
        st.metric("Session String", has_session)

    # ★ 冲突检测
    if CONFIG.login.user_type == 1 and CONFIG.login.BOT_TOKEN and not CONFIG.login.SESSION_STRING:
        st.error(
            "⚠️ **配置冲突**: 账号类型为 User，但只有 Bot Token，没有 Session String！\n\n"
            "请点击上方 Save 按钮重新保存。"
        )
    if CONFIG.login.user_type == 0 and CONFIG.login.SESSION_STRING and not CONFIG.login.BOT_TOKEN:
        st.warning(
            "⚠️ 账号类型为 Bot，但设置了 Session String。Bot Token 为空。\n"
            "请检查是否需要切换为 User 类型。"
        )
