# nb/web_ui/0_👋_Hello.py
"""Home page."""

import streamlit as st

from nb import __version__
from nb.config import read_config
from nb.web_ui.password import check_password
from nb.web_ui.utils import jelly_svg, render_jelly_hero, switch_theme

CONFIG = read_config()

st.set_page_config(page_title="转发管理器", page_icon="👋", layout="wide")
switch_theme(st, CONFIG)

if check_password(st):
    render_jelly_hero(
        st,
        "转发管理器",
        "账号登录、通道连接、插件规则与运行状态管理",
        icon="rocket",
        badge=f"v{__version__}",
    )

    c1, c2, c3 = st.columns(3)
    cards = [
        ("link", "通道连接", "配置源频道、目标频道、评论区同步与分页媒体抓取。", "glass-card"),
        ("bolt", "运行模式", "支持实时监听 (Live)、历史消息搬运 (Past) 与每日定时转发 (Schedule)。", "aqua-card"),
        ("plugin", "插件规则", "配置关键词与媒体过滤、正则替换、页眉页脚、水印、OCR、防剧透与按钮处理。", "iridescent-card"),
    ]
    for col, (icon, title, desc, card_cls) in zip([c1, c2, c3], cards):
        with col:
            st.markdown(
                f"""
                <div class="{card_cls}">
                  {jelly_svg(icon, 46)}
                  <h3 style="margin: 14px 0 8px 0; color: #1d1e2c; font-weight: 800;">{title}</h3>
                  <p style="color: #5a607a; font-size: 0.92em; line-height: 1.55; margin:0;">{desc}</p>
                </div>
                """,
                unsafe_allow_html=True,
            )
