# nb/web_ui/pages/3_🔗_Connections.py

import time
import streamlit as st
from nb.config import CONFIG, Forward, read_config, write_config
from nb.web_ui.password import check_password
from nb.web_ui.utils import get_list, get_string, hide_st, switch_theme

CONFIG = read_config()

st.set_page_config(page_title="Connections", page_icon="🔗")
hide_st(st)
switch_theme(st, CONFIG)

def rerun():
    """兼容旧版 Streamlit 的刷新方法"""
    if hasattr(st, 'rerun'): 
        st.rerun()
    elif hasattr(st, 'experimental_rerun'): 
        st.experimental_rerun()
    else:
        st.warning("Please refresh the page manually.")

def _parse_id(value: str):
    try: 
        return int(value.strip())
    except: 
        return value.strip()

if check_password(st):
    if st.button("Add new connection"):
        CONFIG.forwards.append(Forward())
        write_config(CONFIG)
        rerun()

    num = len(CONFIG.forwards)
    if num > 0:
        # 生成 Tab 标签
        tab_titles = []
        for i, f in enumerate(CONFIG.forwards):
            status = '🟢' if f.use_this else '🟡'
            name = f.con_name or f"Con {i+1}"
            tab_titles.append(f"{status} {name}")
            
        tabs = st.tabs(tab_titles)
        
        for i in range(num):
            with tabs[i]:
                con = i + 1
                # 1. 基础信息
                with st.expander("Metadata"):
                    CONFIG.forwards[i].con_name = st.text_input("Name", value=CONFIG.forwards[i].con_name, key=f"n{con}")
                    CONFIG.forwards[i].use_this = st.checkbox("Active", value=CONFIG.forwards[i].use_this, key=f"u{con}")
                
                # 2. 源和目的地
                with st.expander("Source & Dest"):
                    src_val = st.text_input("Source ID", value=str(CONFIG.forwards[i].source), key=f"s{con}")
                    CONFIG.forwards[i].source = _parse_id(src_val)
                    dest_list = get_list(st.text_area("Destinations", value=get_string(CONFIG.forwards[i].dest), key=f"d{con}"))
                    CONFIG.forwards[i].dest = [_parse_id(d) for d in dest_list]

                # 3. 评论区增强 (修复了 toggle 报错)
                with st.expander("💬 评论区 (Comments)"):
                    f = CONFIG.forwards[i]
                    f.forward_comments = st.checkbox(
                        "转发该消息下的评论", 
                        value=f.forward_comments, 
                        key=f"fc{con}"
                    )
                    if f.forward_comments:
                        # 修复点：将 st.toggle 换成了 st.checkbox，兼容 1.15.2
                        f.comm_only_media = st.checkbox(
                            "仅转发带媒体的评论 (忽略纯文本)", 
                            value=f.comm_only_media, 
                            key=f"com_med_{con}"
                        )
                        if not f.comm_only_media:
                            f.comm_max_text = st.number_input(
                                "每个帖子转发纯文本评论上限", 
                                min_value=0, 
                                max_value=100, 
                                value=f.comm_max_text, 
                                key=f"cmt{con}",
                                help="超过此数量后，只转发带图片/视频的评论。"
                            )

                # 4. 历史模式设置
                with st.expander("Past Mode Settings"):
                    offset_val = st.text_input("Offset ID", value=str(CONFIG.forwards[i].offset), key=f"o{con}")
                    try:
                        CONFIG.forwards[i].offset = int(offset_val) if offset_val else 0
                    except:
                        CONFIG.forwards[i].offset = 0
                        
                    end_val = st.text_input("End ID (Optional)", value=str(CONFIG.forwards[i].end) if CONFIG.forwards[i].end else "", key=f"e{con}")
                    try:
                        CONFIG.forwards[i].end = int(end_val) if end_val else None
                    except:
                        CONFIG.forwards[i].end = None

                # 5. 删除操作
                if st.button(f"Delete Connection {con}", key=f"del{con}"):
                    del CONFIG.forwards[i]
                    write_config(CONFIG)
                    rerun()

    if st.button("Save All Settings"):
        write_config(CONFIG)
        st.success("Configuration Saved!")
        time.sleep(1)
        rerun()
