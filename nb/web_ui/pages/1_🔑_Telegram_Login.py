import os
import streamlit as st
from nb.config import CONFIG, read_config, write_config
from nb.web_ui.password import check_password
from nb.web_ui.utils import hide_st, switch_theme

CONFIG = read_config()
st.set_page_config(page_title="Telegram 登录", page_icon="🔑")
hide_st(st)
switch_theme(st, CONFIG)

if check_password(st):
    env_api_id = os.getenv("API_ID", "")
    env_api_hash = os.getenv("API_HASH", "")
    env_session = os.getenv("SESSION_STRING", "")
    env_bot = os.getenv("BOT_TOKEN", "")

    found = [k for k, v in {"API_ID": env_api_id, "API_HASH": env_api_hash, "SESSION_STRING": env_session, "BOT_TOKEN": env_bot}.items() if v]
    if found:
        st.info(f"🔍 检测到环境变量: {', '.join(found)}，将作为默认值填入。")

    auto_type = CONFIG.login.user_type
    if env_session and not env_bot:
        auto_type = 1
    elif env_bot and not env_session:
        auto_type = 0

    default_api_id = env_api_id or str(CONFIG.login.API_ID)
    default_api_hash = env_api_hash or CONFIG.login.API_HASH
    default_session = env_session or CONFIG.login.SESSION_STRING
    default_bot = env_bot or CONFIG.login.BOT_TOKEN

    input_api_id = st.text_input("API ID", value=default_api_id, type="password")
    input_api_hash = st.text_input("API HASH", value=default_api_hash, type="password")
    st.write("可从 https://my.telegram.org 获取 API ID 和 API HASH。")

    user_type = st.radio("选择账号类型", ["Bot", "User"], index=auto_type)
    input_bot = ""
    input_session = ""

    if user_type == "Bot":
        selected_type = 0
        input_bot = st.text_input("Bot Token", value=default_bot, type="password")
        if not input_bot:
            st.warning("⚠️ Bot Token 为空")
    else:
        selected_type = 1
        input_session = st.text_input("Session String", value=default_session, type="password")
        if input_session:
            ss = input_session.strip()
            if ":" in ss and len(ss) < 100:
                parts = ss.split(":", 1)
                if parts[0].isdigit():
                    st.error("🚫 这看起来是 Bot Token，不是 Session String！\n\n如需使用 Bot 账号请切换类型。")
        if not input_session:
            st.warning("⚠️ Session String 为空")
        with st.expander("如何获取 Session String？"):
            st.markdown("链接: https://replit.com/@artai8/tg-login?v=1\n\n在上述链接中输入 API ID、API HASH 和手机号生成 Session String。")

    if st.button("保存"):
        try:
            save_api_id = int(input_api_id)
        except ValueError:
            st.error("API ID 必须是整数")
            st.stop()
        CONFIG.login.API_ID = save_api_id
        CONFIG.login.API_HASH = input_api_hash.strip()
        CONFIG.login.user_type = selected_type
        if selected_type == 0:
            CONFIG.login.BOT_TOKEN = input_bot.strip()
            CONFIG.login.SESSION_STRING = ""
        else:
            CONFIG.login.SESSION_STRING = input_session.strip()
            CONFIG.login.BOT_TOKEN = ""
        write_config(CONFIG)
        desc = "Bot" if selected_type == 0 else "User"
        cred = input_bot if selected_type == 0 else input_session
        st.success(f"✅ 已保存！账号类型: **{desc}**，凭证: {'已设置 ✅' if cred else '未设置 ❌'}")

    st.markdown("---")
    st.markdown("##### 当前配置状态")
    c1, c2, c3 = st.columns(3)
    with c1:
        st.metric("账号类型", "Bot" if CONFIG.login.user_type == 0 else "User")
    with c2:
        st.metric("Bot Token", "✅" if CONFIG.login.BOT_TOKEN else "❌")
    with c3:
        st.metric("Session String", "✅" if CONFIG.login.SESSION_STRING else "❌")

    if CONFIG.login.user_type == 1 and CONFIG.login.BOT_TOKEN and not CONFIG.login.SESSION_STRING:
        st.error("⚠️ 配置冲突：账号类型为 User，但只有 Bot Token！请重新保存。")
    if CONFIG.login.user_type == 0 and CONFIG.login.SESSION_STRING and not CONFIG.login.BOT_TOKEN:
        st.warning("⚠️ 账号类型为 Bot，但设置了 Session String，Bot Token 为空。")
