# nb/web_ui/pages/1_🔑_Telegram_Login.py

import streamlit as st
import os
import re

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


def _clean_session_string(raw: str) -> str:
    """清理 session string"""
    if not raw:
        return ""
    cleaned = raw.strip()
    for q in ('"', "'", '\u201c', '\u201d', '\u2018', '\u2019'):
        if cleaned.startswith(q) and cleaned.endswith(q):
            cleaned = cleaned[1:-1].strip()
    cleaned = cleaned.replace("\n", "").replace("\r", "").replace(" ", "")
    return cleaned


def _validate_session_string_ui(session_str: str) -> tuple:
    """验证 session string，返回 (is_valid, error_message)"""
    if not session_str:
        return False, "Session string 为空"

    cleaned = _clean_session_string(session_str)

    if not cleaned:
        return False, "清理后 session string 为空"

    if cleaned[0] != '1':
        return False, (
            f"Session string 版本不匹配: 首字符='{cleaned[0]}' (期望 '1')\n"
            f"请确认你使用的是 Telethon v1 生成的 session string"
        )

    expected_lengths = [353, 369]
    if len(cleaned) not in expected_lengths:
        return False, (
            f"Session string 长度异常: {len(cleaned)} (期望 {expected_lengths})\n"
            f"请检查是否完整复制了整个字符串"
        )

    if not re.match(r'^[A-Za-z0-9+/=]+$', cleaned):
        invalid = set(re.findall(r'[^A-Za-z0-9+/=]', cleaned))
        return False, f"包含非法字符: {invalid}"

    # 尝试实际创建 StringSession
    try:
        from telethon.sessions import StringSession
        StringSession(cleaned)
    except ValueError as e:
        return False, f"Telethon 验证失败: {e}"
    except Exception as e:
        return False, f"验证时出错: {e}"

    return True, f"✅ 有效 (长度={len(cleaned)})"


def _validate_bot_token_ui(token: str) -> tuple:
    """验证 bot token，返回 (is_valid, error_message)"""
    if not token:
        return False, "Bot token 为空"

    token = token.strip()
    if ":" not in token:
        return False, "格式无效: 缺少冒号。正确格式: 123456789:ABCdef..."

    parts = token.split(":", 1)
    if not parts[0].isdigit():
        return False, "格式无效: 冒号前应该是数字"

    if len(parts[1]) < 20:
        return False, f"Token 后半部分过短 ({len(parts[1])} 字符)，可能不完整"

    return True, f"✅ 格式正确 (Bot ID: {parts[0]})"


if check_password(st):

    # 从环境变量或配置读取默认值
    env_api_id = os.getenv("API_ID", "")
    env_api_hash = os.getenv("API_HASH", "")
    env_session_string = os.getenv("SESSION_STRING", "")
    env_bot_token = os.getenv("BOT_TOKEN", "")

    default_api_id = env_api_id or str(CONFIG.login.API_ID)
    default_api_hash = env_api_hash or CONFIG.login.API_HASH
    default_session_string = env_session_string or CONFIG.login.SESSION_STRING
    default_bot_token = env_bot_token or CONFIG.login.BOT_TOKEN

    # 输入框
    input_api_id = st.text_input("API ID", value=default_api_id, type="password")
    input_api_hash = st.text_input("API HASH", value=default_api_hash, type="password")

    st.write("You can get api id and api hash from https://my.telegram.org.")

    user_type = st.radio(
        "Choose account type", ["Bot", "User"], index=CONFIG.login.user_type
    )

    if user_type == "Bot":
        CONFIG.login.user_type = 0
        input_bot_token = st.text_input(
            "Enter bot token", value=default_bot_token, type="password"
        )

        # 实时验证 bot token
        if input_bot_token:
            is_valid, msg = _validate_bot_token_ui(input_bot_token)
            if is_valid:
                st.success(msg)
            else:
                st.warning(f"⚠️ {msg}")

        CONFIG.login.BOT_TOKEN = input_bot_token

    else:
        CONFIG.login.user_type = 1
        input_session = st.text_input(
            "Enter session string", value=default_session_string, type="password"
        )

        # 实时验证 session string
        if input_session:
            is_valid, msg = _validate_session_string_ui(input_session)
            if is_valid:
                st.success(msg)
            else:
                st.error(f"❌ {msg}")

        CONFIG.login.SESSION_STRING = input_session

        with st.expander("How to get session string ?"):
            st.markdown(
                """
            Link to repl: https://replit.com/@artai8/tg-login?v=1

            _Click on the above link and enter api id, api hash, and phone no to generate session string._

            **Important notes:**

            - The session string should start with `1` and be ~353 characters long
            - Copy the **entire** string without any spaces or line breaks
            - Do **not** include quotes around the string

            **Note from developer:**

            Due some issues logging in with a user account using a phone no is not supported in this web interface.

            I have built a command-line program named tg-login (https://github.com/artai8/tg-login) that can generate the session string for you.

            You can run tg-login on your computer, or securely in this repl. tg-login is open source, and you can also inspect the bash script running in the repl.

            What is a session string ?
            https://docs.telethon.dev/en/stable/concepts/sessions.html#string-sessions
            """
            )

    # 保存
    if st.button("Save"):
        errors = []

        # 验证 API ID
        try:
            api_id = int(input_api_id)
            if api_id <= 0:
                errors.append("API ID 必须是正整数")
        except ValueError:
            errors.append("API ID 必须是数字")
            api_id = 0

        # 验证 API HASH
        if not input_api_hash.strip():
            errors.append("API HASH 不能为空")

        # 验证登录凭证
        if user_type == "User":
            cleaned = _clean_session_string(input_session)
            if cleaned:
                is_valid, msg = _validate_session_string_ui(cleaned)
                if not is_valid:
                    errors.append(f"Session String: {msg}")
                else:
                    CONFIG.login.SESSION_STRING = cleaned  # 保存清理后的版本
            else:
                errors.append("Session String 为空")
        else:
            if not input_bot_token.strip():
                errors.append("Bot Token 为空")

        if errors:
            for err in errors:
                st.error(f"❌ {err}")
        else:
            CONFIG.login.API_ID = api_id
            CONFIG.login.API_HASH = input_api_hash.strip()
            write_config(CONFIG)
            st.success("✅ Configuration saved successfully!")
            st.balloons()
