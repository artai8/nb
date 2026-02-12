import os
import streamlit as st
import yaml
from nb.config import CONFIG, read_config, write_config
from nb.plugin_models import FileType, Replace, Style, InlineButtonMode
from nb.web_ui.password import check_password
from nb.web_ui.utils import get_list, get_string, hide_st, switch_theme

CONFIG = read_config()
st.set_page_config(page_title="插件", page_icon="🔌")
hide_st(st)
switch_theme(st, CONFIG)

if check_password(st):
    with st.expander("过滤器"):
        CONFIG.plugins.filter.check = st.checkbox("启用过滤器插件", value=CONFIG.plugins.filter.check)
        st.write("通过黑名单或白名单过滤特定内容。")
        text_tab, users_tab, files_tab = st.tabs(["文本", "用户", "文件"])
        with text_tab:
            CONFIG.plugins.filter.text.case_sensitive = st.checkbox("区分大小写", value=CONFIG.plugins.filter.text.case_sensitive)
            CONFIG.plugins.filter.text.regex = st.checkbox("使用正则表达式", value=CONFIG.plugins.filter.text.regex)
            st.write("每行输入一个表达式")
            CONFIG.plugins.filter.text.whitelist = get_list(st.text_area("文本白名单", value=get_string(CONFIG.plugins.filter.text.whitelist)))
            CONFIG.plugins.filter.text.blacklist = get_list(st.text_area("文本黑名单", value=get_string(CONFIG.plugins.filter.text.blacklist)))
        with users_tab:
            st.write("每行输入一个用户名或 ID")
            CONFIG.plugins.filter.users.whitelist = get_list(st.text_area("用户白名单", value=get_string(CONFIG.plugins.filter.users.whitelist)))
            CONFIG.plugins.filter.users.blacklist = get_list(st.text_area("用户黑名单", value=get_string(CONFIG.plugins.filter.users.blacklist)))
        flist = [item.value for item in FileType]
        with files_tab:
            CONFIG.plugins.filter.files.whitelist = st.multiselect("文件白名单", flist, default=CONFIG.plugins.filter.files.whitelist)
            CONFIG.plugins.filter.files.blacklist = st.multiselect("文件黑名单", flist, default=CONFIG.plugins.filter.files.blacklist)

    with st.expander("格式化"):
        CONFIG.plugins.fmt.check = st.checkbox("启用格式化插件", value=CONFIG.plugins.fmt.check)
        st.write("为文本添加样式，如 **粗体**、_斜体_、~~删除线~~、`等宽` 等。")
        style_list = [item.value for item in Style]
        CONFIG.plugins.fmt.style = st.selectbox("格式", style_list, index=style_list.index(CONFIG.plugins.fmt.style))

    with st.expander("水印"):
        if os.system("ffmpeg -version >> /dev/null 2>&1") != 0:
            st.warning("未找到 `ffmpeg`，使用此插件需要安装 `ffmpeg`。")
        CONFIG.plugins.mark.check = st.checkbox("为媒体添加水印（图片和视频）", value=CONFIG.plugins.mark.check)
        uploaded_file = st.file_uploader("上传水印图片(png)", type=["png"])
        if uploaded_file is not None:
            with open("image.png", "wb") as f:
                f.write(uploaded_file.getbuffer())

    with st.expander("OCR"):
        st.write("光学字符识别")
        if os.system("tesseract --version >> /dev/null 2>&1") != 0:
            st.warning("未找到 `tesseract`，使用此插件需要安装 `tesseract`。")
        CONFIG.plugins.ocr.check = st.checkbox("对图片启用 OCR", value=CONFIG.plugins.ocr.check)
        st.write("识别出的文本将添加到图片描述中。")

    with st.expander("替换"):
        CONFIG.plugins.replace.check = st.checkbox("启用文本替换", value=CONFIG.plugins.replace.check)
        CONFIG.plugins.replace.regex = st.checkbox("使用正则表达式", value=CONFIG.plugins.replace.regex)
        CONFIG.plugins.replace.text_raw = st.text_area("替换规则", value=CONFIG.plugins.replace.text_raw)
        try:
            replace_dict = yaml.safe_load(CONFIG.plugins.replace.text_raw)
            if not replace_dict:
                replace_dict = {}
            temp = Replace(text=replace_dict)
            del temp
        except Exception as err:
            st.error(err)
            CONFIG.plugins.replace.text = {}
        else:
            CONFIG.plugins.replace.text = replace_dict
        if st.checkbox("显示用法说明"):
            st.markdown(
                "将一个词或表达式替换为另一个。\n\n"
                "- 每行写一条替换规则\n"
                "- 格式: `'原文': '新文本'`\n"
                "- 建议使用**单引号**\n"
                "- 包含空格或特殊字符时必须使用引号\n\n"
                "```\n'原文': '新文本'\n```"
            )

    with st.expander("标题"):
        CONFIG.plugins.caption.check = st.checkbox("启用标题插件", value=CONFIG.plugins.caption.check)
        CONFIG.plugins.caption.header = st.text_area("页眉", value=CONFIG.plugins.caption.header)
        CONFIG.plugins.caption.footer = st.text_area("页脚", value=CONFIG.plugins.caption.footer)
        st.write("页眉和页脚中可以包含空行，以增加与原文之间的间距。")

    with st.expander("发送者"):
        st.write("使用其他账号发送转发的消息")
        st.warning("必须禁用"显示转发来源"选项，否则消息将无法发送", icon="⚠️")
        CONFIG.plugins.sender.check = st.checkbox("设置发送者:", value=CONFIG.plugins.sender.check)
        leftpad, content, rightpad = st.columns([0.05, 0.9, 0.05])
        with content:
            user_type = st.radio("账号类型", ["Bot", "User"], index=CONFIG.plugins.sender.user_type, horizontal=True)
            if user_type == "Bot":
                CONFIG.plugins.sender.user_type = 0
                CONFIG.plugins.sender.BOT_TOKEN = st.text_input("Bot Token", value=CONFIG.plugins.sender.BOT_TOKEN, type="password")
            else:
                CONFIG.plugins.sender.user_type = 1
                CONFIG.plugins.sender.SESSION_STRING = st.text_input("Session String", CONFIG.plugins.sender.SESSION_STRING, type="password")
                with st.expander("如何获取 Session String？"):
                    st.markdown("链接: https://replit.com/@artai8/tg-login?v=1\n\n在上述链接中输入 API ID、API HASH 和手机号生成 Session String。")

    with st.expander("内联按钮"):
        st.write("控制转发消息时如何处理内联按钮。")
        CONFIG.plugins.inline.check = st.checkbox("启用内联按钮处理", value=CONFIG.plugins.inline.check)
        if CONFIG.plugins.inline.check:
            mode_options = [item.value for item in InlineButtonMode]
            mode_labels = {"remove": "🗑️ 移除 — 完全去除所有内联按钮", "replace_url": "🔗 替换URL — 保留按钮，仅替换URL", "replace_all": "✏️ 全部替换 — 替换按钮文本和URL"}
            current_mode = CONFIG.plugins.inline.mode
            if hasattr(current_mode, 'value'):
                current_mode = current_mode.value
            current_index = mode_options.index(current_mode) if current_mode in mode_options else 0
            selected_mode = st.selectbox("按钮处理模式", mode_options, index=current_index, format_func=lambda x: mode_labels.get(x, x))
            CONFIG.plugins.inline.mode = selected_mode
            if selected_mode in ("replace_url", "replace_all"):
                st.markdown("---")
                st.markdown("##### URL 替换")
                st.write("替换按钮URL中的部分内容，YAML格式: `'旧URL': '新URL'`")
                CONFIG.plugins.inline.url_replacements_raw = st.text_area("URL 替换规则", value=CONFIG.plugins.inline.url_replacements_raw, key="inline_url_repl")
                try:
                    url_repl = yaml.safe_load(CONFIG.plugins.inline.url_replacements_raw)
                    if not url_repl:
                        url_repl = {}
                    if not isinstance(url_repl, dict):
                        raise ValueError("必须是 YAML 字典格式")
                    CONFIG.plugins.inline.url_replacements = {str(k): str(v) for k, v in url_repl.items()}
                except Exception as err:
                    st.error(f"URL 替换规则错误: {err}")
                    CONFIG.plugins.inline.url_replacements = {}
                st.caption("示例:")
                st.code("'https://old-domain.com': 'https://new-domain.com'\n'?ref=abc': '?ref=xyz'", language="yaml")
            if selected_mode == "replace_all":
                st.markdown("---")
                st.markdown("##### 按钮文本替换")
                st.write("替换按钮文本，YAML格式: `'旧文本': '新文本'`")
                CONFIG.plugins.inline.text_replacements_raw = st.text_area("文本替换规则", value=CONFIG.plugins.inline.text_replacements_raw, key="inline_text_repl")
                try:
                    text_repl = yaml.safe_load(CONFIG.plugins.inline.text_replacements_raw)
                    if not text_repl:
                        text_repl = {}
                    if not isinstance(text_repl, dict):
                        raise ValueError("必须是 YAML 字典格式")
                    CONFIG.plugins.inline.text_replacements = {str(k): str(v) for k, v in text_repl.items()}
                except Exception as err:
                    st.error(f"文本替换规则错误: {err}")
                    CONFIG.plugins.inline.text_replacements = {}
                st.caption("示例:")
                st.code("'Buy Now': '立即购买'\n'Subscribe': '订阅'", language="yaml")
        else:
            st.info("禁用时，内联按钮将被**自动移除**以防止转发错误。")

    if st.button("保存"):
        write_config(CONFIG)
