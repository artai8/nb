import streamlit as st
import yaml
from nb.config import CONFIG, Forward, read_config, write_config
from nb.web_ui.password import check_password
from nb.web_ui.utils import get_list, get_string, hide_st, switch_theme

CONFIG = read_config()
st.set_page_config(page_title="转发连接", page_icon="🔗")
hide_st(st)
switch_theme(st, CONFIG)


def rerun():
    if hasattr(st, 'rerun'):
        st.rerun()
    elif hasattr(st, 'experimental_rerun'):
        st.experimental_rerun()
    else:
        raise st.script_runner.StopException


def _parse_id(value: str):
    value = value.strip()
    if not value:
        return ""
    try:
        return int(value)
    except ValueError:
        pass
    if "t.me/" in value:
        parts = value.split("t.me/")
        if len(parts) == 2:
            name = parts[1].strip().rstrip("/")
            if name.startswith("+"):
                return value
            if name:
                return f"@{name}" if not name.startswith("@") else name
    if value.startswith("@"):
        return value
    if value.isascii() and not value.startswith("-"):
        return f"@{value}"
    return value


def _display_id(value) -> str:
    if value is None or value == "":
        return ""
    return str(value)


def _safe_int(value, default=0):
    if value is None:
        return default
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


if check_password(st):
    st.info(
        "💡 **推荐使用用户名格式**，例如：\n"
        "- `@channel_name`（公开频道/群组用户名）\n"
        "- `https://t.me/channel_name`（t.me 链接）\n"
        "- `-1001234567890`（数字 ID）"
    )
    add_new = st.button("添加新连接")
    if add_new:
        CONFIG.forwards.append(Forward())
        write_config(CONFIG)
    num = len(CONFIG.forwards)
    if num == 0:
        st.write("暂无转发连接，点击上方按钮创建。")
    else:
        tab_strings = []
        for i in range(num):
            label = CONFIG.forwards[i].con_name or f"连接 {i+1}"
            status = "🟢" if CONFIG.forwards[i].use_this else "🟡"
            if CONFIG.forwards[i].comments.enabled:
                status += "💬"
            tab_strings.append(f"{status} {label}")
        tabs = st.tabs(list(tab_strings))
        for i in range(num):
            with tabs[i]:
                con = i + 1
                name = CONFIG.forwards[i].con_name
                label = f"{con} [{name}]" if name else con
                with st.expander("基本设置"):
                    st.write(f"连接 ID: **{con}**")
                    CONFIG.forwards[i].con_name = st.text_input("连接名称", value=CONFIG.forwards[i].con_name, key=con)
                    CONFIG.forwards[i].use_this = st.checkbox("启用此连接", value=CONFIG.forwards[i].use_this, key=f"use {con}")
                with st.expander("源和目标"):
                    st.write(f"配置连接 {label}")
                    source_input = st.text_input("源", value=_display_id(CONFIG.forwards[i].source), key=f"source {con}", help="输入 @用户名、t.me 链接或数字 ID").strip()
                    CONFIG.forwards[i].source = _parse_id(source_input)
                    parsed_source = CONFIG.forwards[i].source
                    if parsed_source:
                        if isinstance(parsed_source, int):
                            st.caption(f"📌 解析为数字 ID: `{parsed_source}`")
                        elif isinstance(parsed_source, str) and parsed_source.startswith("@"):
                            st.caption(f"📌 解析为用户名: `{parsed_source}`")
                        else:
                            st.caption(f"📌 解析为: `{parsed_source}`")
                    st.write("每个连接只允许一个源")
                    raw_dest = get_list(st.text_area("目标", value=get_string(CONFIG.forwards[i].dest), key=f"dest {con}", help="每行一个"))
                    CONFIG.forwards[i].dest = [_parse_id(item) for item in raw_dest]
                    if CONFIG.forwards[i].dest:
                        parsed_list = ", ".join([f"`{d}`" for d in CONFIG.forwards[i].dest if d])
                        st.caption(f"📌 目标解析为: {parsed_list}")
                    st.write("每行写一个目标")
                with st.expander("💬 评论区转发"):
                    st.markdown(
                        "**评论区转发**: 将源频道帖子评论转发到目标频道帖子评论区。\n\n"
                        "**前提条件:**\n"
                        "- 源和目标频道都需开启评论功能\n"
                        "- 主帖子需先完成转发\n"
                        "- 建议使用 User 账号"
                    )
                    comments = CONFIG.forwards[i].comments
                    comments.enabled = st.checkbox("启用评论区转发", value=comments.enabled, key=f"comments_enabled {con}")
                    if comments.enabled:
                        st.markdown("---")
                        st.markdown("##### 源设置")
                        comments.source_mode = st.radio("评论获取方式", ["comments", "discussion"], index=0 if comments.source_mode == "comments" else 1, key=f"comments_src_mode {con}", help="**comments**: 自动发现讨论组\n\n**discussion**: 手动指定讨论组 ID")
                        if comments.source_mode == "discussion":
                            dg_input = st.text_input("源讨论组 ID", value=_display_id(comments.source_discussion_group), key=f"comments_src_dg {con}").strip()
                            comments.source_discussion_group = _parse_id(dg_input) if dg_input else None
                        st.markdown("---")
                        st.markdown("##### 目标设置")
                        comments.dest_mode = st.radio("评论发送方式", ["comments", "discussion"], index=0 if comments.dest_mode == "comments" else 1, key=f"comments_dest_mode {con}", help="**comments**: 自动发送到目标帖子评论区（推荐）\n\n**discussion**: 直接发送到指定讨论组")
                        if comments.dest_mode == "discussion":
                            raw_dg = get_list(st.text_area("目标讨论组 ID（每行一个）", value=get_string(comments.dest_discussion_groups), key=f"comments_dest_dgs {con}"))
                            comments.dest_discussion_groups = [_parse_id(item) for item in raw_dg]
                        st.markdown("---")
                        st.markdown("##### 过滤选项")
                        comments.only_media = st.checkbox("仅转发包含媒体的评论", value=comments.only_media, key=f"comments_only_media {con}")
                        comments.include_text_comments = st.checkbox("包含纯文本评论", value=comments.include_text_comments, key=f"comments_text {con}")
                        comments.skip_bot_comments = st.checkbox("跳过机器人评论", value=comments.skip_bot_comments, key=f"comments_skip_bot {con}")
                        comments.skip_admin_comments = st.checkbox("跳过管理员评论", value=comments.skip_admin_comments, key=f"comments_skip_admin {con}")
                        st.markdown("---")
                        st.markdown("##### 帖子映射")
                        comments.post_mapping_mode = st.radio("帖子映射模式", ["auto", "manual"], index=0 if comments.post_mapping_mode != "manual" else 1, key=f"comments_mapping_mode {con}", help="**auto**: 转发帖子时自动建立映射（推荐）\n\n**manual**: 手动指定映射关系")
                        if comments.post_mapping_mode == "manual":
                            comments.manual_post_mapping_raw = st.text_area("手动帖子映射（YAML格式: 源帖子ID: 目标帖子ID）", value=comments.manual_post_mapping_raw, key=f"comments_manual_map {con}")
                            try:
                                mapping = yaml.safe_load(comments.manual_post_mapping_raw)
                                if not mapping:
                                    mapping = {}
                                if not isinstance(mapping, dict):
                                    raise ValueError("必须是 YAML 字典格式")
                                comments.manual_post_mapping = {str(k): str(v) for k, v in mapping.items()}
                            except Exception as err:
                                st.error(f"映射格式错误: {err}")
                                comments.manual_post_mapping = {}
                            st.caption("示例:")
                            st.code("123: 456\n789: 1011", language="yaml")
                    CONFIG.forwards[i].comments = comments
                with st.expander("Past 模式设置"):
                    CONFIG.forwards[i].offset = _safe_int(st.text_input("偏移量", value=str(CONFIG.forwards[i].offset), key=f"offset {con}"), default=0)
                    end_input = st.text_input("结束位置", value=str(CONFIG.forwards[i].end) if CONFIG.forwards[i].end is not None else "", key=f"end {con}")
                    CONFIG.forwards[i].end = _safe_int(end_input, default=None) if end_input.strip() else None
                with st.expander("删除此连接"):
                    st.warning(f'点击"删除"将永久删除连接 {label}，此操作不可撤销。', icon="⚠️")
                    if st.button(f"删除连接 {label}"):
                        del CONFIG.forwards[i]
                        write_config(CONFIG)
                        rerun()
    if st.button("保存"):
        write_config(CONFIG)
        rerun()
