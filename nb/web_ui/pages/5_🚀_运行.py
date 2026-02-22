# nb/web_ui/pages/5_🏃_Run.py

import os
import signal
import subprocess
import sys
import time
# ✅ 新增：导入 html 库用于转义特殊字符
import html

import streamlit as st
import streamlit.components.v1 as components
from nb.config import CONFIG, read_config, write_config
from nb.web_ui.password import check_password
from nb.web_ui.utils import switch_theme

CONFIG = read_config()

PID_FILE = os.path.join(os.getcwd(), "nb.pid")
LOG_FILE = os.path.join(os.getcwd(), "logs.txt")
OLD_LOG_FILE = os.path.join(os.getcwd(), "old_logs.txt")

# --- Process Utils (保持不变) ---
def rerun():
    if hasattr(st, 'rerun'): st.rerun()
    elif hasattr(st, 'experimental_rerun'): st.experimental_rerun()
    else: st.warning("Refresh needed")

def _read_pid_file() -> int:
    try:
        if os.path.exists(PID_FILE):
            with open(PID_FILE, "r") as f:
                s = f.read().strip()
                if s: return int(s)
    except: pass
    return 0

def _write_pid_file(pid: int):
    with open(PID_FILE, "w") as f: f.write(str(pid))

def _remove_pid_file():
    if os.path.exists(PID_FILE):
        try: os.remove(PID_FILE)
        except: pass

def is_process_alive(pid: int) -> bool:
    if pid <= 0: return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError: return False
    except PermissionError: return True
    except OSError: return False

def get_running_pid() -> int:
    f_pid = _read_pid_file()
    c_pid = CONFIG.pid
    if f_pid > 0 and is_process_alive(f_pid):
        if c_pid != f_pid:
            CONFIG.pid = f_pid
            write_config(CONFIG)
        return f_pid
    if c_pid > 0 and is_process_alive(c_pid):
        _write_pid_file(c_pid)
        return c_pid
    if f_pid > 0 or c_pid > 0:
        _remove_pid_file()
        if c_pid > 0:
            CONFIG.pid = 0
            write_config(CONFIG)
    return 0

def _kill_posix(pid: int, force: bool) -> bool:
    if not is_process_alive(pid):
        _remove_pid_file()
        return True
    try:
        if force:
            os.killpg(pid, signal.SIGKILL)
        else:
            os.killpg(pid, signal.SIGTERM)
    except Exception:
        try:
            os.kill(pid, signal.SIGKILL if force else signal.SIGTERM)
        except Exception:
            pass
    time.sleep(2 if not force else 1)
    if not is_process_alive(pid):
        _remove_pid_file()
        return True
    if not force:
        try:
            os.killpg(pid, signal.SIGKILL)
        except Exception:
            try:
                os.kill(pid, signal.SIGKILL)
            except Exception:
                pass
        time.sleep(1)
    res = not is_process_alive(pid)
    if res:
        _remove_pid_file()
    return res


def _kill_windows(pid: int, force: bool) -> bool:
    if not is_process_alive(pid):
        _remove_pid_file()
        return True
    flag = "/F" if force else ""
    try:
        os.system(f"taskkill /PID {pid} /T {flag}")
    except Exception:
        pass
    time.sleep(1)
    res = not is_process_alive(pid)
    if res:
        _remove_pid_file()
    return res


def kill_process(pid: int, force: bool = False) -> bool:
    if not is_process_alive(pid):
        _remove_pid_file()
        return True
    if os.name == "nt":
        return _kill_windows(pid, force)
    return _kill_posix(pid, force)

def start_nb_process(mode: str) -> int:
    if os.path.exists(LOG_FILE):
        try: os.rename(LOG_FILE, OLD_LOG_FILE)
        except: pass
    cwd = os.getcwd()
    python = sys.executable
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONPATH"] = cwd
    cmd = [python, "-u", "-m", "nb.cli", mode, "--loud"]
    try:
        fd = os.open(LOG_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
        proc = subprocess.Popen(cmd, stdout=fd, stderr=fd, stdin=subprocess.DEVNULL, cwd=cwd, env=env, start_new_session=True)
        os.close(fd)
        time.sleep(2)
        if proc.poll() is not None: return 0
        _write_pid_file(proc.pid)
        return proc.pid
    except: return 0

def termination():
    _remove_pid_file()
    CONFIG.pid = 0
    write_config(CONFIG)

# --- UI Code ---

st.set_page_config(page_title="Run Dashboard", page_icon="🏃", layout="wide")
switch_theme(st, CONFIG)

if check_password(st):
    
    # CSS for Status Card & Terminal (Neumorphism Enhanced)
    st.markdown("""
    <style>
    .status-card {
        background: linear-gradient(135deg, rgba(16, 185, 129, 0.85) 0%, rgba(5, 150, 105, 0.85) 100%);
        backdrop-filter: blur(5px);
        color: white;
        padding: 2rem;
        border-radius: 20px;
        text-align: center;
        box-shadow:  9px 9px 16px var(--shadow-dark),
                    -9px -9px 16px var(--shadow-light);
        border: 1px solid rgba(255,255,255,0.2);
    }
    .status-stopped {
        background: linear-gradient(135deg, rgba(239, 68, 68, 0.85) 0%, rgba(220, 38, 38, 0.85) 100%);
    }
    .pulse {
        width: 12px; height: 12px; background: white; border-radius: 50%;
        display: inline-block; margin-right: 8px;
        box-shadow: 0 0 0 0 rgba(255, 255, 255, 0.7);
        animation: pulse-animation 2s infinite;
    }
    @keyframes pulse-animation {
        0% { box-shadow: 0 0 0 0 rgba(255, 255, 255, 0.7); }
        70% { box-shadow: 0 0 0 10px rgba(255, 255, 255, 0); }
        100% { box-shadow: 0 0 0 0 rgba(255, 255, 255, 0); }
    }
    
    /* Terminal Wrapper */
    .terminal-wrapper {
        background: #1e293b; /* Dark background for terminal */
        border-radius: 15px;
        box-shadow:  9px 9px 16px var(--shadow-dark),
                    -9px -9px 16px var(--shadow-light);
        overflow: hidden;
        border: 1px solid var(--glass-border);
    }
    
    .terminal-head {
        background: #0f172a;
        padding: 12px 20px;
        display: flex; gap: 8px; align-items: center;
        border-bottom: 1px solid #334155;
    }
    
    .terminal-body {
        padding: 20px;
        height: 400px;
        overflow-y: auto;
        color: #e2e8f0;
        font-family: 'Consolas', 'Monaco', monospace;
        font-size: 13px;
        white-space: pre-wrap;
        box-shadow: inset 0 0 20px rgba(0,0,0,0.2); /* Inner shadow for depth */
    }

    .dot { width: 12px; height: 12px; border-radius: 50%; }
    .red { background: #ef4444; box-shadow: 0 0 5px #ef4444; } 
    .yellow { background: #f59e0b; box-shadow: 0 0 5px #f59e0b; } 
    .green { background: #10b981; box-shadow: 0 0 5px #10b981; }
    </style>
    """, unsafe_allow_html=True)

    st.title("运营仪表盘")
    
    col_main, col_stat = st.columns([2, 1])
    pid = get_running_pid()

    with col_stat:
        if pid > 0:
            st.markdown(f"""
            <div class="status-card">
                <h2 style="color:white; margin:0;">运行中</h2>
                <div style="margin-top:10px; opacity:0.9;">
                    <span class="pulse"></span> 进程 ID: {pid}
                </div>
            </div>
            """, unsafe_allow_html=True)
        else:
            st.markdown(f"""
            <div class="status-card status-stopped">
                <h2 style="color:white; margin:0;">已停止</h2>
                <div style="margin-top:10px; opacity:0.9;">没有活动进程</div>
            </div>
            """, unsafe_allow_html=True)

    with col_main:
        with st.container():
            c1, c2, c3 = st.columns(3)
            with c1:
                CONFIG.show_forwarded_from = st.checkbox("显示 “转发自”", value=CONFIG.show_forwarded_from)
            with c2:
                # 模式映射：0->live(居住), 1->past(过去的)
                mode_label = "居住" if CONFIG.mode == 0 else "过去的"
                mode = st.radio("模式", ["居住", "过去的"], index=CONFIG.mode, horizontal=True, label_visibility="collapsed")
            with c3:
                if mode == "过去的":
                    CONFIG.mode = 1
                else:
                    CONFIG.live.delete_sync = st.checkbox("同步删除", value=CONFIG.live.delete_sync)
                    CONFIG.mode = 0
        
        st.write("---")
        
        if pid == 0:
            # 修复：移除 use_container_width=True
            if st.button("▶️ 开始流程", type="primary"):
                # 传入 "live" 或 "past" 对应的英文参数
                mode_arg = "live" if CONFIG.mode == 0 else "past"
                new_pid = start_nb_process(mode_arg)
                if new_pid > 0:
                    CONFIG.pid = new_pid
                    write_config(CONFIG)
                    time.sleep(1)
                    rerun()
                else:
                    st.error("启动失败")
            # 占位，保持布局一致
            st.empty()
        else:
            k1, k2 = st.columns([1, 3])
            with k1:
                # 修复：移除 use_container_width=True
                if st.button("⏹️ 停止", type="primary"):
                    if kill_process(pid):
                        termination()
                        time.sleep(1)
                        rerun()
            with k2:
                # 修复：移除 use_container_width=True
                if st.button("🔴 强制终止", type="secondary"):
                    os.system(f"kill -9 {pid}")
                    termination()
                    time.sleep(1)
                    rerun()

    # --- Terminal Log ---
    st.write("")
    
    # 按钮与刷新设置行
    c_act1, c_act2, c_act3, c_act4 = st.columns([1, 1, 1, 1])
    
    with c_act1:
        if st.button("🔄 刷新日志", use_container_width=True):
            rerun()

    with c_act2:
        refresh_interval = st.selectbox(
            "间隔 (秒)",
            [1, 2, 3, 5, 10],
            index=1,
            label_visibility="collapsed",
            disabled=False,
        )

    with c_act3:
        st.write("")  # Spacer
        auto_refresh = st.toggle("自动刷新", value=False)

    with c_act4:
        st.caption("刷新间隔 (秒)")

    log_content = "暂无日志。"
    if os.path.exists(LOG_FILE):
        try:
            with open(LOG_FILE, "r") as f:
                lines = f.readlines()
                raw_content = "".join(lines[-100:]) if lines else "等待输出..."
                # ✅ 关键修复：转义 HTML 字符，防止破坏 DOM 结构
                log_content = html.escape(raw_content)
        except: pass
    
    # 恢复日志显示框样式（黑色背景）
    st.components.v1.html(
        f"""
        <div id="log-container" style="height:400px; overflow-y:auto; padding:16px; background:#1e293b; color:#e2e8f0; font-family:Consolas, Monaco, monospace; font-size:13px; white-space:pre-wrap; border-radius:15px; border:1px solid rgba(255,255,255,0.1);">
            {log_content}
        </div>
        <script>
            const box = document.getElementById('log-container');
            if (box) {{
                box.scrollTop = box.scrollHeight;
            }}
        </script>
        """,
        height=420,
        scrolling=False
    )
    if auto_refresh:
        time.sleep(refresh_interval)
        rerun()
