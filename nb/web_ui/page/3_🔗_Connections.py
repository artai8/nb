import time

import streamlit as st

from nb.config import CONFIG, Forward, read_config, write_config
from nb.web_ui.password import check_password
from nb.web_ui.utils import get_list, get_string, hide_st, switch_theme

CONFIG = read_config()

st.set_page_config(
    page_title="Connections",
    page_icon="🔗",
)
hide_st(st)
switch_theme(st, CONFIG)


def _parse_id(value: str):
    """尝试将字符串转为 int，失败则保持字符串。"""
    value = value.strip()
    try:
        return int(value)
    except ValueError:
        return value


def _safe_int(value, default=0):
    """安全地将值转为 int，处理 None 和非数字字符串。"""
    if value is None:
        return default
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


if check_password(st):
    add_new = st.button("Add new connection")
    if add_new:
        CONFIG.forwards.append(Forward())
        write_config(CONFIG)

    num = len(CONFIG.forwards)

    if num == 0:
        st.write(
            "No connections found. Click on Add new connection above to create one!"
        )
    else:
        tab_strings = []
        for i in range(num):
            if CONFIG.forwards[i].con_name:
                label = CONFIG.forwards[i].con_name
            else:
                label = f"Connection {i+1}"
            if CONFIG.forwards[i].use_this:
                status = "🟢"
            else:
                status = "🟡"

            tab_strings.append(f"{status} {label}")

        tabs = st.tabs(list(tab_strings))

        for i in range(num):
            with tabs[i]:
                con = i + 1
                name = CONFIG.forwards[i].con_name
                if name:
                    label = f"{con} [{name}]"
                else:
                    label = con
                with st.expander("Modify Metadata"):
                    st.write(f"Connection ID: **{con}**")
                    CONFIG.forwards[i].con_name = st.text_input(
                        "Name of this connection",
                        value=CONFIG.forwards[i].con_name,
                        key=con,
                    )

                    st.info(
                        "You can untick the below checkbox to suspend this connection."
                    )
                    CONFIG.forwards[i].use_this = st.checkbox(
                        "Use this connection",
                        value=CONFIG.forwards[i].use_this,
                        key=f"use {con}",
                    )
                with st.expander("Source and Destination"):
                    st.write(f"Configure connection {label}")

                    source_input = st.text_input(
                        "Source",
                        value=str(CONFIG.forwards[i].source),
                        key=f"source {con}",
                    ).strip()
                    CONFIG.forwards[i].source = _parse_id(source_input)
                    st.write("only one source is allowed in a connection")

                    raw_dest = get_list(
                        st.text_area(
                            "Destinations",
                            value=get_string(CONFIG.forwards[i].dest),
                            key=f"dest {con}",
                        )
                    )
                    CONFIG.forwards[i].dest = [_parse_id(item) for item in raw_dest]
                    st.write("Write destinations one item per line")

                with st.expander("Past Mode Settings"):
                    CONFIG.forwards[i].offset = _safe_int(  # 修复：安全转换
                        st.text_input(
                            "Offset",
                            value=str(CONFIG.forwards[i].offset),
                            key=f"offset {con}",
                        ),
                        default=0,
                    )
                    end_input = st.text_input(  # 修复：安全处理 None
                        "End",
                        value=str(CONFIG.forwards[i].end) if CONFIG.forwards[i].end is not None else "",
                        key=f"end {con}",
                    )
                    CONFIG.forwards[i].end = _safe_int(end_input, default=None) if end_input.strip() else None
                with st.expander("Delete this connection"):
                    st.warning(
                        f"Clicking the 'Remove' button will **delete** connection **{label}**. This action cannot be reversed once done.",
                        icon="⚠️",
                    )

                    if st.button(f"Remove connection **{label}**"):
                        del CONFIG.forwards[i]
                        write_config(CONFIG)
                        st.rerun()

    if st.button("Save"):
        write_config(CONFIG)
        st.rerun()