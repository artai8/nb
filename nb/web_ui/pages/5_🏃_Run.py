# nb/web_ui/pages/5_🏃_Run.py 完整代码

import os
import signal
import subprocess
import sys
import time
import streamlit as st
from nb.config import CONFIG, read_config, write_config
from nb.web_ui.password import check_password
from nb.web_ui.utils import hide_st, switch_theme

CONFIG = read_config()

st.set_page_config(page_title="Run", page_icon="🏃")
hide_st(st)
switch_theme(st, CONFIG)

if check_password(st):
    with st.expander("运行配置"):
        CONFIG.show_forwarded_from = st.checkbox("保留 'Forwarded from'", value=CONFIG.show_forwarded_from)
        m = st.radio("选择模式", ["live", "past"], index=CONFIG.mode)
        CONFIG.mode = 0 if m == "live" else 1
        if st.button("保存并更新配置"):
            write_config(CONFIG)
            st.success("配置已保存")

    if CONFIG.pid == 0:
        if st.button("🚀 启动 nb", type="primary"):
            logs = open("logs.txt", "w")
            # 使用 -u 参数确保 python 输出不带缓存，实时写入日志
            process = subprocess.Popen(
                [sys.executable, "-u", "-m", "nb.cli", "past" if CONFIG.mode==1 else "live", "--loud"],
                stdout=logs, stderr=subprocess.STDOUT
            )
            CONFIG.pid = process.pid
            write_config(CONFIG)
            st.info(f"正在启动进程 (PID: {CONFIG.pid})...")
            time.sleep(2)
            st.experimental_rerun()
    else:
        st.success(f"✅ nb 正在运行 (PID: {CONFIG.pid})")
        if st.button("🛑 停止 nb", type="primary"):
            try:
                os.kill(CONFIG.pid, signal.SIGTERM)
            except: pass
            CONFIG.pid = 0
            write_config(CONFIG)
            st.warning("进程已停止")
            st.experimental_rerun()

    st.markdown("### 实时日志 (最新 100 行)")
    if os.path.exists("logs.txt"):
        with open("logs.txt", "r") as f:
            lines = f.readlines()
            st.code("".join(lines[-100:]))
    else:
        st.write("暂无日志文件")
    
    if st.button("刷新日志"):
        st.experimental_rerun()
