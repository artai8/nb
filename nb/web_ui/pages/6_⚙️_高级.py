# nb/web_ui/pages/6_⚙️_高级.py
"""Advanced System Settings & Fail Log Recovery."""

import os
import json
import streamlit as st

from nb.const import FAILED_FORWARD_LOG_FILE_NAME
from nb.config import read_config
from nb.web_ui.password import check_password
from nb.web_ui.utils import render_jelly_hero, switch_theme

CONFIG = read_config()

st.set_page_config(page_title="高级设置", page_icon="⚙️", layout="wide")
switch_theme(st, CONFIG)

if check_password(st):
    render_jelly_hero(
        st,
        "高级设置",
        "失败记录重试与系统配置导入导出",
        icon="settings",
    )
