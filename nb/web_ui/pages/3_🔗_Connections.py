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
    if hasattr(st, 'rerun'): st.rerun()
    else: st.experimental_rerun()

def _parse_id(value: str):
    try: return int(value.strip())
    except: return value.strip()

if check_password(st):
    if st.button("Add new connection"):
        CONFIG.forwards.append(Forward())
        write_config(CONFIG)
        rerun()

    num = len(CONFIG.forwards)
    if num > 0:
        tabs = st.tabs([f"{'🟢' if f.use_this else '🟡'} {f.con_name or f'Con {i+1}'}" for i, f in enumerate(CONFIG.forwards)])
        for i in range(num):
            with tabs[i]:
                con = i + 1
                with st.expander("Metadata"):
                    CONFIG.forwards[i].con_name = st.text_input("Name", value=CONFIG.forwards[i].con_name, key=f"n{con}")
                    CONFIG.forwards[i].use_this = st.checkbox("Active", value=CONFIG.forwards[i].use_this, key=f"u{con}")
                
                with st.expander("Source & Dest"):
                    src = st.text_input("Source ID", value=str(CONFIG.forwards[i].source), key=f"s{con}")
                    CONFIG.forwards[i].source = _parse_id(src)
                    dest_list = get_list(st.text_area("Destinations", value=get_string(CONFIG.forwards[i].dest), key=f"d{con}"))
                    CONFIG.forwards[i].dest = [_parse_id(d) for d in dest_list]

                with st.expander("💬 评论区 (Comments)"):
                    f = CONFIG.forwards[i]
                    f.forward_comments = st.checkbox("转发该消息下的评论", value=f.forward_comments, key=f"fc{con}")
                    if f.forward_comments:
                        f.comm_only_media = st.toggle("仅转发带媒体的评论", value=f.comm_only_media, key=f"com{con}")
                        if not f.comm_only_media:
                            f.comm_max_text = st.number_input("每个帖子转发纯文本评论上限", 0, 100, f.comm_max_text, key=f"cmt{con}")

                with st.expander("Past Mode Settings"):
                    CONFIG.forwards[i].offset = int(st.text_input("Offset ID", value=str(CONFIG.forwards[i].offset), key=f"o{con}") or 0)
                    end = st.text_input("End ID (Optional)", value=str(CONFIG.forwards[i].end) if CONFIG.forwards[i].end else "", key=f"e{con}")
                    CONFIG.forwards[i].end = int(end) if end else None

                if st.button(f"Delete Connection {con}", key=f"del{con}"):
                    del CONFIG.forwards[i]
                    write_config(CONFIG)
                    rerun()

    if st.button("Save All Settings"):
        write_config(CONFIG)
        st.success("Saved!")
        rerun()
