import time

import streamlit as st
import yaml

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


def rerun():
    """兼容不同版本的 Streamlit rerun 方法"""
    if hasattr(st, 'rerun'):
        st.rerun()
    elif hasattr(st, 'experimental_rerun'):
        st.experimental_rerun()
    else:
        raise st.script_runner.StopException


def _parse_id(value: str):
    value = value.strip()
    try:
        return int(value)
    except ValueError:
        return value


def _safe_int(value, default=0):
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

            # 评论区状态标识
            if CONFIG.forwards[i].comments.enabled:
                status += "💬"

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

                # ==================== 评论区配置 ====================
                with st.expander("💬 Comments / Discussion"):
                    st.markdown("""
                    **评论区转发**: 从源频道帖子的评论区获取消息，转发到目标频道帖子的评论区。

                    **前提条件:**
                    - 源频道和目标频道都需要开启评论功能（关联讨论组）
                    - 主帖子需要先完成转发（评论区功能基于帖子映射）
                    - 建议使用用户账号（bot 可能无法访问讨论组）
                    """)

                    comments = CONFIG.forwards[i].comments

                    comments.enabled = st.checkbox(
                        "启用评论区转发",
                        value=comments.enabled,
                        key=f"comments_enabled {con}",
                    )

                    if comments.enabled:
                        st.markdown("---")
                        st.markdown("##### 源设置")

                        comments.source_mode = st.radio(
                            "评论获取方式",
                            ["comments", "discussion"],
                            index=0 if comments.source_mode == "comments" else 1,
                            key=f"comments_src_mode {con}",
                            help=(
                                "**comments**: 自动发现源频道的讨论组\n\n"
                                "**discussion**: 手动指定源讨论组 ID"
                            ),
                        )

                        if comments.source_mode == "discussion":
                            dg_input = st.text_input(
                                "源讨论组 ID",
                                value=str(comments.source_discussion_group or ""),
                                key=f"comments_src_dg {con}",
                            ).strip()
                            comments.source_discussion_group = _parse_id(dg_input) if dg_input else None

                        st.markdown("---")
                        st.markdown("##### 目标设置")

                        comments.dest_mode = st.radio(
                            "评论发送方式",
                            ["comments", "discussion"],
                            index=0 if comments.dest_mode == "comments" else 1,
                            key=f"comments_dest_mode {con}",
                            help=(
                                "**comments**: 自动发送到目标频道帖子的评论区（推荐）\n\n"
                                "**discussion**: 直接发送到指定讨论组"
                            ),
                        )

                        if comments.dest_mode == "discussion":
                            raw_dg = get_list(
                                st.text_area(
                                    "目标讨论组 ID（每行一个）",
                                    value=get_string(comments.dest_discussion_groups),
                                    key=f"comments_dest_dgs {con}",
                                )
                            )
                            comments.dest_discussion_groups = [
                                _parse_id(item) for item in raw_dg
                            ]

                        st.markdown("---")
                        st.markdown("##### 过滤选项")

                        comments.only_media = st.checkbox(
                            "仅转发包含媒体的评论",
                            value=comments.only_media,
                            key=f"comments_only_media {con}",
                        )

                        comments.include_text_comments = st.checkbox(
                            "包含纯文本评论",
                            value=comments.include_text_comments,
                            key=f"comments_text {con}",
                        )

                        comments.skip_bot_comments = st.checkbox(
                            "跳过机器人发的评论",
                            value=comments.skip_bot_comments,
                            key=f"comments_skip_bot {con}",
                        )

                        comments.skip_admin_comments = st.checkbox(
                            "跳过管理员发的评论",
                            value=comments.skip_admin_comments,
                            key=f"comments_skip_admin {con}",
                        )

                        st.markdown("---")
                        st.markdown("##### 帖子映射")

                        comments.post_mapping_mode = st.radio(
                            "帖子映射模式",
                            ["auto", "manual"],
                            index=0 if comments.post_mapping_mode != "manual" else 1,
                            key=f"comments_mapping_mode {con}",
                            help=(
                                "**auto**: 转发帖子时自动建立映射（推荐）\n\n"
                                "**manual**: 手动指定源帖子ID到目标帖子ID的对应关系"
                            ),
                        )

                        if comments.post_mapping_mode == "manual":
                            comments.manual_post_mapping_raw = st.text_area(
                                "手动帖子映射（YAML格式: 源帖子ID: 目标帖子ID）",
                                value=comments.manual_post_mapping_raw,
                                key=f"comments_manual_map {con}",
                            )
                            try:
                                mapping = yaml.safe_load(
                                    comments.manual_post_mapping_raw
                                )
                                if not mapping:
                                    mapping = {}
                                if not isinstance(mapping, dict):
                                    raise ValueError("必须是 YAML 字典格式")
                                comments.manual_post_mapping = {
                                    str(k): str(v) for k, v in mapping.items()
                                }
                            except Exception as err:
                                st.error(f"映射格式错误: {err}")
                                comments.manual_post_mapping = {}

                            st.caption("示例:")
                            st.code(
                                "123: 456\n789: 1011",
                                language="yaml",
                            )

                    CONFIG.forwards[i].comments = comments

                with st.expander("Past Mode Settings"):
                    CONFIG.forwards[i].offset = _safe_int(
                        st.text_input(
                            "Offset",
                            value=str(CONFIG.forwards[i].offset),
                            key=f"offset {con}",
                        ),
                        default=0,
                    )
                    end_input = st.text_input(
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
                        rerun()

    if st.button("Save"):
        write_config(CONFIG)
        rerun()
