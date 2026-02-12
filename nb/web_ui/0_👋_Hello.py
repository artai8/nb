import streamlit as st
from nb.web_ui.utils import hide_st, switch_theme
from nb.config import read_config

CONFIG = read_config()
st.set_page_config(page_title="欢迎", page_icon="👋")
hide_st(st)
switch_theme(st, CONFIG)
st.write("# 欢迎使用 nb 👋")

html = '<p align="center"><img src="https://user-images.githubusercontent.com/66209958/115183360-3fa4d500-a0f9-11eb-9c0f-c5ed03a9ae17.png" alt="nb logo" width=120></p>'
st.components.v1.html(html, width=None, height=None, scrolling=False)

with st.expander("功能介绍"):
    st.markdown("""
nb 是一款自动化 Telegram 消息转发工具。

主要功能：
- 转发消息（保留来源或发送副本）
- 支持 past（历史消息）和 live（实时消息）两种模式
- 支持 Bot 和 User 账号登录
- 丰富的插件系统：过滤、格式化、替换、水印、OCR 等
- 支持评论区同步转发
- Web 管理界面
    """)

st.warning("修改配置后请点击"保存"按钮。")
