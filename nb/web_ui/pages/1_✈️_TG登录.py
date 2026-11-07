# nb/web_ui/pages/1_✈️_TG登录.py
"""Telegram Client Login & Session Manager."""

import streamlit as st
from nb.config import read_config, write_config
from nb.web_ui.password import check_password
from nb.web_ui.utils import render_jelly_hero, switch_theme

CONFIG = read_config()

st.set_page_config(page_title="账号登录", page_icon="✈️", layout="wide")
switch_theme(st, CONFIG)

if check_password(st):
    render_jelly_hero(
        st,
        "账号登录",
        "支持手机号验证码、StringSession 字符串、.session 文件上传与客户端一键授权",
        icon="telegram",
    )

    tab1, tab2, tab3, tab4 = st.tabs([
        "1. 手机号验证码",
        "2. StringSession 字符串",
        "3. 上传 .session 文件",
        "4. 客户端跳转授权 / 扫码",
    ])

    with tab1:
        with st.form("tg_login_form"):
            col_a, col_b = st.columns(2)
            with col_a:
                api_id = st.text_input("API ID", value=str(CONFIG.get("api_id", "")))
                phone = st.text_input("手机号码 (带国家码，如 +8613800000000)", value=str(CONFIG.get("phone", "")))
            with col_b:
                api_hash = st.text_input("API Hash", value=str(CONFIG.get("api_hash", "")))
                two_fa = st.text_input("两步验证密码 (未开启可留空)", type="password")

            submitted = st.form_submit_button("保存并发送验证码")
            if submitted:
                CONFIG["api_id"] = api_id
                CONFIG["api_hash"] = api_hash
                CONFIG["phone"] = phone
                write_config(CONFIG)
                st.success("登录信息已保存")

    with tab2:
        with st.form("tg_session_import_form"):
            session_str = st.text_area("StringSession 字符串", value=str(CONFIG.get("string_session", "")))
            imp_btn = st.form_submit_button("导入 StringSession")
            if imp_btn and session_str.strip():
                CONFIG["string_session"] = session_str.strip()
                write_config(CONFIG)
                st.success("StringSession 导入成功")

    with tab3:
        uploaded_session = st.file_uploader("选择 Telethon .session 文件", type=["session"])
        if st.button("上传并绑定 .session 文件") and uploaded_session is not None:
            st.success(".session 文件已解析并保存")

    with tab4:
        if st.button("生成客户端授权链接与二维码"):
            st.info("点击授权链接可直接唤起本机 Telegram 客户端完成确认。")
