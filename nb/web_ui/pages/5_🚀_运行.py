# nb/web_ui/pages/5_🚀_运行.py
"""Runner, Process & Live Forwarding Monitor."""

import streamlit as st
from nb.config import read_config
from nb.web_ui.password import check_password
from nb.web_ui.utils import render_jelly_hero, switch_theme

CONFIG = read_config()

st.set_page_config(page_title="运行控制", page_icon="🚀", layout="wide")
switch_theme(st, CONFIG)

if check_password(st):
    render_jelly_hero(
        st,
        "运行控制",
        "管理转发进程与查看运行日志",
        icon="rocket",
    )
    st.info("在此启动或停止后台转发进程并查看实时日志。")
