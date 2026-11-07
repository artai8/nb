# nb/web_ui/pages/4_🧩_插件.py
"""Plugin Engine Management."""

import streamlit as st
from nb.config import read_config, write_config
from nb.web_ui.password import check_password
from nb.web_ui.utils import render_jelly_hero, switch_theme

CONFIG = read_config()

FILE_TYPES = [
    "audio",
    "gif",
    "video",
    "video_note",
    "sticker",
    "contact",
    "photo",
    "document",
    "nofile",
]

FORMAT_STYLES = ["preserve", "bold", "italic", "strikethrough", "spoiler", "code"]
WATERMARK_POSITIONS = ["centre", "top_left", "top_right", "bottom_left", "bottom_right"]
INLINE_MODES = ["remove", "replace_url", "replace_all"]


def _lines_to_list(text: str) -> list[str]:
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


def _list_to_lines(items: list) -> str:
    return "\n".join(str(x) for x in (items or []))


def _raw_to_dict(raw: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in _lines_to_list(raw):
        sep = ":" if ":" in line else ("=" if "=" in line else "")
        if sep:
            k, v = line.split(sep, 1)
            if k.strip():
                result[k.strip()] = v.strip()
    return result


def _dict_to_raw(mapping: dict) -> str:
    if not isinstance(mapping, dict):
        return ""
    return "\n".join(f"{k}: {v}" for k, v in mapping.items())


st.set_page_config(page_title="插件配置", page_icon="🧩", layout="wide")
switch_theme(st, CONFIG)

if check_password(st):
    render_jelly_hero(
        st,
        "插件配置",
        "配置过滤、替换、页眉页脚、按钮处理、水印、排版、OCR 与防剧透规则",
        icon="plugin",
    )

    plugins_cfg = CONFIG.setdefault("plugins", {})
    filter_cfg = plugins_cfg.setdefault("filter", {})
    replace_cfg = plugins_cfg.setdefault("replace", {})
    caption_cfg = plugins_cfg.setdefault("caption", {})
    fmt_cfg = plugins_cfg.setdefault("fmt", {})
    mark_cfg = plugins_cfg.setdefault("mark", {})
    ocr_cfg = plugins_cfg.setdefault("ocr", {})
    spoiler_cfg = plugins_cfg.setdefault("spoiler", {})
    inline_cfg = plugins_cfg.setdefault("inline", {})

    with st.form("full_plugins_config_form"):
        col_left, col_right = st.columns(2)

        with col_left:
            st.markdown('<div class="glass-card"><strong>1. 过滤规则</strong></div>', unsafe_allow_html=True)
            filter_check = st.toggle("启用过滤", value=bool(filter_cfg.get("check", False)))

            users_cfg = filter_cfg.get("users", {})
            files_cfg = filter_cfg.get("files", {})
            text_cfg = filter_cfg.get("text", {})

            f_c1, f_c2 = st.columns(2)
            with f_c1:
                text_whitelist = st.text_area("关键词白名单 (每行一个)", value=_list_to_lines(text_cfg.get("whitelist", [])), height=90)
                users_whitelist = st.text_area("发送者白名单 (每行一个)", value=_list_to_lines(users_cfg.get("whitelist", [])), height=75)
            with f_c2:
                text_blacklist = st.text_area("关键词黑名单 (每行一个)", value=_list_to_lines(text_cfg.get("blacklist", [])), height=90)
                users_blacklist = st.text_area("发送者黑名单 (每行一个)", value=_list_to_lines(users_cfg.get("blacklist", [])), height=75)

            files_blacklist = st.multiselect(
                "禁止转发的媒体类型 (黑名单)",
                options=FILE_TYPES,
                default=[x for x in files_cfg.get("blacklist", []) if x in FILE_TYPES],
            )
            files_whitelist = st.multiselect(
                "仅允许转发的媒体类型 (白名单，留空不限)",
                options=FILE_TYPES,
                default=[x for x in files_cfg.get("whitelist", []) if x in FILE_TYPES],
            )

        with col_right:
            st.markdown('<div class="iridescent-card"><strong>2. 文本替换</strong></div>', unsafe_allow_html=True)
            replace_check = st.toggle("启用文本替换", value=bool(replace_cfg.get("check", False)))
            replace_regex = st.checkbox("使用正则表达式匹配", value=bool(replace_cfg.get("regex", False)))
            replace_raw_default = replace_cfg.get("text_raw") or _dict_to_raw(replace_cfg.get("text", {}))
            replace_text_raw = st.text_area(
                "替换规则 (每行一条 `原内容: 新内容`，冒号右侧留空即删除)",
                value=replace_raw_default,
                height=220,
            )

        st.markdown("<br>", unsafe_allow_html=True)
        col_l2, col_r2 = st.columns(2)

        with col_l2:
            st.markdown('<div class="aqua-card"><strong>3. 页眉与页脚</strong></div>', unsafe_allow_html=True)
            caption_check = st.toggle("启用页眉与页脚", value=bool(caption_cfg.get("check", False)))
            caption_header = st.text_area("顶部页眉", value=str(caption_cfg.get("header", "")), height=85)
            caption_footer = st.text_area("底部页脚", value=str(caption_cfg.get("footer", "")), height=85)

        with col_r2:
            st.markdown('<div class="glass-card"><strong>4. 消息内联按钮</strong></div>', unsafe_allow_html=True)
            inline_check = st.toggle("启用按钮处理", value=bool(inline_cfg.get("check", False)))
            cur_mode = inline_cfg.get("mode", "remove")
            inline_mode = st.selectbox(
                "处理方式",
                options=INLINE_MODES,
                index=INLINE_MODES.index(cur_mode) if cur_mode in INLINE_MODES else 0,
            )
            inline_url_raw = st.text_area(
                "按钮链接替换 (`旧URL: 新URL`)",
                value=inline_cfg.get("url_replacements_raw") or _dict_to_raw(inline_cfg.get("url_replacements", {})),
                height=70,
            )
            inline_text_raw = st.text_area(
                "按钮文字替换 (`旧文字: 新文字`)",
                value=inline_cfg.get("text_replacements_raw") or _dict_to_raw(inline_cfg.get("text_replacements", {})),
                height=70,
            )

        st.markdown("<br>", unsafe_allow_html=True)
        col_l3, col_r3 = st.columns(2)

        with col_l3:
            st.markdown('<div class="glass-card"><strong>5. 图片与视频水印</strong></div>', unsafe_allow_html=True)
            mark_check = st.toggle("启用水印", value=bool(mark_cfg.get("check", False)))
            mark_image = st.text_input("水印图片路径", value=str(mark_cfg.get("image", "image.png")))
            cur_pos = str(mark_cfg.get("position", "centre"))
            mark_position = st.selectbox(
                "水印位置",
                options=WATERMARK_POSITIONS,
                index=WATERMARK_POSITIONS.index(cur_pos) if cur_pos in WATERMARK_POSITIONS else 0,
            )
            mark_fps = st.number_input("视频帧率 (FPS)", min_value=1, max_value=60, value=int(mark_cfg.get("frame_rate", 15)))

        with col_r3:
            st.markdown('<div class="iridescent-card"><strong>6~8. 排版样式 · OCR · 防剧透</strong></div>', unsafe_allow_html=True)
            fmt_check = st.toggle("6. 启用文本排版样式", value=bool(fmt_cfg.get("check", False)))
            cur_style = str(fmt_cfg.get("style", "preserve"))
            fmt_style = st.selectbox(
                "排版样式",
                options=FORMAT_STYLES,
                index=FORMAT_STYLES.index(cur_style) if cur_style in FORMAT_STYLES else 0,
            )

            ocr_check = st.toggle("7. 启用图片 OCR 识别过滤", value=bool(ocr_cfg.get("check", False)))
            ocr_lang = st.text_input("OCR 语言代码 (如 chi_sim+eng)", value=str(ocr_cfg.get("lang", "chi_sim")))

            spoiler_check = st.toggle("8. 启用媒体防剧透遮罩", value=bool(spoiler_cfg.get("check", False)))

        save_all = st.form_submit_button("保存插件设置")
        if save_all:
            CONFIG["plugins"] = {
                "filter": {
                    "check": filter_check,
                    "users": {
                        "whitelist": _lines_to_list(users_whitelist),
                        "blacklist": _lines_to_list(users_blacklist),
                    },
                    "files": {
                        "whitelist": files_whitelist,
                        "blacklist": files_blacklist,
                    },
                    "text": {
                        "whitelist": _lines_to_list(text_whitelist),
                        "blacklist": _lines_to_list(text_blacklist),
                    },
                },
                "replace": {
                    "check": replace_check,
                    "regex": replace_regex,
                    "text_raw": replace_text_raw,
                    "text": _raw_to_dict(replace_text_raw),
                },
                "caption": {
                    "check": caption_check,
                    "header": caption_header,
                    "footer": caption_footer,
                },
                "inline": {
                    "check": inline_check,
                    "mode": inline_mode,
                    "url_replacements_raw": inline_url_raw,
                    "url_replacements": _raw_to_dict(inline_url_raw),
                    "text_replacements_raw": inline_text_raw,
                    "text_replacements": _raw_to_dict(inline_text_raw),
                },
                "mark": {
                    "check": mark_check,
                    "image": mark_image.strip() or "image.png",
                    "position": mark_position,
                    "frame_rate": int(mark_fps),
                },
                "fmt": {
                    "check": fmt_check,
                    "style": fmt_style,
                },
                "ocr": {
                    "check": ocr_check,
                    "lang": ocr_lang.strip() or "chi_sim",
                },
                "spoiler": {
                    "check": spoiler_check,
                },
            }
            write_config(CONFIG)
            st.success("插件配置已保存")
