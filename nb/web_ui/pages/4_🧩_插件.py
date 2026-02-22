import os

import streamlit as st
import yaml

from nb.config import CONFIG, read_config, write_config
from nb.plugin_models import FileType, Replace, Style, InlineButtonMode
from nb.web_ui.password import check_password
from nb.web_ui.utils import get_list, get_string, hide_st, switch_theme

CONFIG = read_config()

st.set_page_config(
    page_title="插件",
    page_icon="🔌",
)

hide_st(st)
switch_theme(st, CONFIG)
if check_password(st):

    with st.expander("过滤器"):
        CONFIG.plugins.filter.check = st.checkbox(
            "启用此插件：过滤器", value=CONFIG.plugins.filter.check
        )
        st.write("黑名单或白名单特定文本项。")
        text_tab, users_tab, files_tab = st.tabs(["文本", "用户", "文件"])

        with text_tab:
            CONFIG.plugins.filter.text.case_sensitive = st.checkbox(
                "区分大小写", value=CONFIG.plugins.filter.text.case_sensitive
            )
            CONFIG.plugins.filter.text.regex = st.checkbox(
                "将过滤器解释为正则表达式", value=CONFIG.plugins.filter.text.regex
            )

            st.write("每行输入一个文本表达式")
            CONFIG.plugins.filter.text.whitelist = get_list(
                st.text_area(
                    "文本白名单",
                    value=get_string(CONFIG.plugins.filter.text.whitelist),
                )
            )
            CONFIG.plugins.filter.text.blacklist = get_list(
                st.text_area(
                    "文本黑名单",
                    value=get_string(CONFIG.plugins.filter.text.blacklist),
                )
            )

        with users_tab:
            st.write("每行输入一个用户名/ID")
            CONFIG.plugins.filter.users.whitelist = get_list(
                st.text_area(
                    "用户白名单",
                    value=get_string(CONFIG.plugins.filter.users.whitelist),
                )
            )
            CONFIG.plugins.filter.users.blacklist = get_list(
                st.text_area(
                    "用户黑名单", get_string(CONFIG.plugins.filter.users.blacklist)
                )
            )

        flist = [item.value for item in FileType]
        with files_tab:
            CONFIG.plugins.filter.files.whitelist = st.multiselect(
                "文件白名单", flist, default=CONFIG.plugins.filter.files.whitelist
            )
            CONFIG.plugins.filter.files.blacklist = st.multiselect(
                "文件黑名单", flist, default=CONFIG.plugins.filter.files.blacklist
            )

    with st.expander("格式化"):
        CONFIG.plugins.fmt.check = st.checkbox(
            "启用此插件：格式化", value=CONFIG.plugins.fmt.check
        )
        st.write(
            "为文本添加样式，如 **粗体**、_斜体_、~~删除线~~、`等宽` 等。"
        )
        style_list = [item.value for item in Style]
        CONFIG.plugins.fmt.style = st.selectbox(
            "格式", style_list, index=style_list.index(CONFIG.plugins.fmt.style)
        )

    with st.expander("水印"):
        if os.system("ffmpeg -version >> /dev/null 2>&1") != 0:
            st.warning(
                "无法找到 `ffmpeg`。请确保服务器已安装 `ffmpeg` 以使用此插件。"
            )
        CONFIG.plugins.mark.check = st.checkbox(
            "对媒体（图片和视频）应用水印。",
            value=CONFIG.plugins.mark.check,
        )
        uploaded_file = st.file_uploader("上传水印图片 (png)", type=["png"])
        if uploaded_file is not None:
            with open("image.png", "wb") as f:
                f.write(uploaded_file.getbuffer())

    with st.expander("OCR 文字识别"):
        st.write("光学字符识别。")
        if os.system("tesseract --version >> /dev/null 2>&1") != 0:
            st.warning(
                "无法找到 `tesseract`。请确保服务器已安装 `tesseract` 以使用此插件。"
            )
        CONFIG.plugins.ocr.check = st.checkbox(
            "启用图片 OCR", value=CONFIG.plugins.ocr.check
        )
        
        lang_options = ["chi_sim", "chi_tra", "eng", "jpn", "kor", "rus"]
        lang_labels = {
            "chi_sim": "简体中文 (chi_sim)",
            "chi_tra": "繁体中文 (chi_tra)",
            "eng": "English (eng)",
            "jpn": "日本語 (jpn)",
            "kor": "한국어 (kor)",
            "rus": "Русский (rus)",
        }
        current_lang = getattr(CONFIG.plugins.ocr, "lang", "chi_sim")
        # 如果当前配置的语言不在列表中，添加到列表
        if current_lang not in lang_options:
            lang_options.append(current_lang)
            
        CONFIG.plugins.ocr.lang = st.selectbox(
            "OCR 语言",
            lang_options,
            index=lang_options.index(current_lang),
            format_func=lambda x: lang_labels.get(x, x),
        )

        st.write("转发时文本将添加到图片描述中。")

    with st.expander("替换"):
        CONFIG.plugins.replace.check = st.checkbox(
            "应用文本替换", value=CONFIG.plugins.replace.check
        )
        CONFIG.plugins.replace.regex = st.checkbox(
            "解释为正则表达式", value=CONFIG.plugins.replace.regex
        )

        CONFIG.plugins.replace.text_raw = st.text_area(
            "替换规则", value=CONFIG.plugins.replace.text_raw
        )
        try:
            replace_dict = yaml.safe_load(
                CONFIG.plugins.replace.text_raw
            )
            if not replace_dict:
                replace_dict = {}
            temp = Replace(text=replace_dict)
            del temp
        except Exception as err:
            st.error(err)
            CONFIG.plugins.replace.text = {}
        else:
            CONFIG.plugins.replace.text = replace_dict

        if st.checkbox("显示规则和用法"):
            st.markdown(
                """
                将一个词或表达式替换为另一个。

                - 每行写一个替换规则。
                - 原始文本后跟 **一个冒号 `:`**，然后是 **一个空格**，最后是新文本。
                - 建议使用 **单引号**。如果字符串包含空格或特殊字符，则必须使用引号。
                - 如果您的正则表达式包含字符 `\`，双引号将不起作用。
                    ```
                    '原始文本': '新文本'

                    ```
                - 查看 [文档](https://github.com/artai8/nb/wiki/Replace-Plugin) 了解高级用法。"""
            )

    with st.expander("标题/页脚"):
        CONFIG.plugins.caption.check = st.checkbox(
            "应用标题/页脚", value=CONFIG.plugins.caption.check
        )
        CONFIG.plugins.caption.header = st.text_area(
            "页眉", value=CONFIG.plugins.caption.header
        )
        CONFIG.plugins.caption.footer = st.text_area(
            "页脚", value=CONFIG.plugins.caption.footer
        )
        st.write(
            "您可以在页眉和页脚中包含空行，以便在原始消息和标题/页脚之间留出空间。"
        )

    with st.expander("发送者"):
        st.write("修改转发消息的发送者（除当前用户/机器人外）")
        st.warning("'显示转发来源' 选项必须禁用，否则消息将无法发送", icon="⚠️")
        CONFIG.plugins.sender.check = st.checkbox(
            "设置发送者为：", value=CONFIG.plugins.sender.check
        )
        leftpad, content, rightpad = st.columns([0.05, 0.9, 0.05])
        with content:
            user_type = st.radio("账户类型", ["机器人 (Bot)", "用户 (User)"], index=CONFIG.plugins.sender.user_type, horizontal=True)
            if user_type == "机器人 (Bot)":
                CONFIG.plugins.sender.user_type = 0
                CONFIG.plugins.sender.BOT_TOKEN = st.text_input(
                    "机器人 Token", value=CONFIG.plugins.sender.BOT_TOKEN, type="password"
                )
            else:
                CONFIG.plugins.sender.user_type = 1
                CONFIG.plugins.sender.SESSION_STRING = st.text_input(
                    "Session String", CONFIG.plugins.sender.SESSION_STRING, type="password"
                )
                st.markdown(
                    """
                <div class="glass-card">
                    <h6 style="margin-top:0">如何获取 Session String？</h6>
                    <p>Replit 链接: <a href="https://replit.com/@artai8/tg-login?v=1" target="_blank">https://replit.com/@artai8/tg-login?v=1</a></p>
                    <p style="margin-bottom:1em"><i>点击上方链接并输入 API ID、API Hash 和手机号以生成 Session String。</i></p>
                    
                    <div style="background:rgba(0,0,0,0.05); padding:10px; border-radius:8px; font-size:0.9em">
                        <strong>开发者提示：</strong><br>
                        由于某些问题，此 Web 界面不支持直接使用手机号登录用户账户。<br>
                        我已经构建了一个名为 tg-login (https://github.com/artai8/tg-login) 的命令行程序，它可以为您生成 Session String。<br>
                        您可以在您的计算机上运行 tg-login，或者在上述 Replit 中安全地运行。tg-login 是开源的，您也可以检查在 Replit 中运行的 bash 脚本。<br>
                        <br>
                        <a href="https://docs.telethon.dev/en/stable/concepts/sessions.html#string-sessions" target="_blank">什么是 Session String？</a>
                    </div>
                </div>
                """,
                    unsafe_allow_html=True,
                )

    # ==================== 新增: Inline Buttons ====================
    with st.expander("内联按钮"):
        st.write("控制转发消息时如何处理内联按钮。")

        CONFIG.plugins.inline.check = st.checkbox(
            "启用内联按钮处理",
            value=CONFIG.plugins.inline.check,
        )

        if CONFIG.plugins.inline.check:
            mode_options = [item.value for item in InlineButtonMode]
            mode_labels = {
                "remove": "🗑️ 移除 — 完全移除所有内联按钮",
                "replace_url": "🔗 替换 URL — 保留按钮，仅替换 URL",
                "replace_all": "✏️ 替换全部 — 替换按钮文本和 URL",
            }

            current_mode = CONFIG.plugins.inline.mode
            if hasattr(current_mode, 'value'):
                current_mode = current_mode.value
            current_index = mode_options.index(current_mode) if current_mode in mode_options else 0

            selected_mode = st.selectbox(
                "按钮处理模式",
                mode_options,
                index=current_index,
                format_func=lambda x: mode_labels.get(x, x),
            )
            CONFIG.plugins.inline.mode = selected_mode

            if selected_mode in ("replace_url", "replace_all"):
                st.markdown("---")
                st.markdown("##### URL 替换")
                st.write("替换按钮 URL 的部分内容。请使用 YAML 格式编写：`'旧 URL 部分': '新 URL 部分'`")
                CONFIG.plugins.inline.url_replacements_raw = st.text_area(
                    "URL 替换规则",
                    value=CONFIG.plugins.inline.url_replacements_raw,
                    key="inline_url_repl",
                )
                try:
                    url_repl = yaml.safe_load(CONFIG.plugins.inline.url_replacements_raw)
                    if not url_repl:
                        url_repl = {}
                    if not isinstance(url_repl, dict):
                        raise ValueError("必须是 YAML 字典")
                    CONFIG.plugins.inline.url_replacements = {
                        str(k): str(v) for k, v in url_repl.items()
                    }
                except Exception as err:
                    st.error(f"URL 替换错误: {err}")
                    CONFIG.plugins.inline.url_replacements = {}

                st.caption("示例:")
                st.code("'https://old-domain.com': 'https://new-domain.com'\n'?ref=abc': '?ref=xyz'", language="yaml")

            if selected_mode == "replace_all":
                st.markdown("---")
                st.markdown("##### 按钮文本替换")
                st.write("替换按钮文本。请使用 YAML 格式编写：`'旧文本': '新文本'`")
                CONFIG.plugins.inline.text_replacements_raw = st.text_area(
                    "文本替换规则",
                    value=CONFIG.plugins.inline.text_replacements_raw,
                    key="inline_text_repl",
                )
                try:
                    text_repl = yaml.safe_load(CONFIG.plugins.inline.text_replacements_raw)
                    if not text_repl:
                        text_repl = {}
                    if not isinstance(text_repl, dict):
                        raise ValueError("必须是 YAML 字典")
                    CONFIG.plugins.inline.text_replacements = {
                        str(k): str(v) for k, v in text_repl.items()
                    }
                except Exception as err:
                    st.error(f"文本替换错误: {err}")
                    CONFIG.plugins.inline.text_replacements = {}

                st.caption("示例:")
                st.code("'Buy Now': 'Shop Here'\n'Subscribe': 'Follow'", language="yaml")

        else:
            st.info(
                "当禁用时，内联按钮将被 **自动移除** "
                "以防止转发错误。"
            )

    if st.button("保存"):
        write_config(CONFIG)
