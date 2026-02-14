# nb/web_ui/pages/5_🏃_Run.py

import os
import signal
import subprocess
import sys
import time
import atexit

import streamlit as st

from nb.config import CONFIG, read_config, write_config
from nb.web_ui.password import check_password
from nb.web_ui.utils import hide_st, switch_theme

CONFIG = read_config()

# PID 文件路径（独立于 Streamlit session）
PID_FILE = os.path.join(os.getcwd(), "nb.pid")
LOG_FILE = os.path.join(os.getcwd(), "logs.txt")
OLD_LOG_FILE = os.path.join(os.getcwd(), "old_logs.txt")


def rerun():
    if hasattr(st, 'rerun'):
        st.rerun()
    elif hasattr(st, 'experimental_rerun'):
        st.experimental_rerun()
    else:
        st.warning("Please refresh the page manually.")


def _read_pid_file() -> int:
    """从 PID 文件读取进程 ID"""
    try:
        if os.path.exists(PID_FILE):
            with open(PID_FILE, "r") as f:
                pid_str = f.read().strip()
                if pid_str:
                    return int(pid_str)
    except (ValueError, IOError):
        pass
    return 0


def _write_pid_file(pid: int):
    """写入 PID 到文件"""
    with open(PID_FILE, "w") as f:
        f.write(str(pid))


def _remove_pid_file():
    """删除 PID 文件"""
    try:
        if os.path.exists(PID_FILE):
            os.remove(PID_FILE)
    except Exception:
        pass


def is_process_alive(pid: int) -> bool:
    """跨平台检查进程是否存活"""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def get_running_pid() -> int:
    """获取当前运行中的 nb 进程 PID。
    同时检查 PID 文件和 CONFIG，以两者中实际存活的为准。
    """
    # 优先检查 PID 文件
    file_pid = _read_pid_file()
    config_pid = CONFIG.pid

    # 检查 PID 文件中的进程
    if file_pid > 0 and is_process_alive(file_pid):
        # 同步到 CONFIG
        if config_pid != file_pid:
            CONFIG.pid = file_pid
            write_config(CONFIG)
        return file_pid

    # 检查 CONFIG 中的进程
    if config_pid > 0 and is_process_alive(config_pid):
        _write_pid_file(config_pid)
        return config_pid

    # 都不存活，清理
    if file_pid > 0 or config_pid > 0:
        _remove_pid_file()
        if config_pid > 0:
            CONFIG.pid = 0
            write_config(CONFIG)

    return 0


def kill_process(pid: int) -> bool:
    """安全终止进程"""
    if not is_process_alive(pid):
        _remove_pid_file()
        return True
    try:
        os.kill(pid, signal.SIGTERM)
        for _ in range(10):
            time.sleep(0.5)
            if not is_process_alive(pid):
                _remove_pid_file()
                return True
        # 强制终止
        try:
            os.kill(pid, signal.SIGKILL)
            time.sleep(1)
        except ProcessLookupError:
            pass
        _remove_pid_file()
        return not is_process_alive(pid)
    except ProcessLookupError:
        _remove_pid_file()
        return True
    except Exception as e:
        st.error(f"终止进程失败: {e}")
        return False


def start_nb_process(mode: str) -> int:
    """启动 nb 进程，完全脱离 Streamlit。

    使用 shell 脚本方式启动，确保：
    1. 进程完全独立于 Streamlit
    2. stdout/stderr 写入日志文件
    3. PID 写入文件
    4. 浏览器关闭/刷新不影响进程
    """
    # 备份旧日志
    if os.path.exists(LOG_FILE):
        try:
            os.rename(LOG_FILE, OLD_LOG_FILE)
        except Exception:
            pass

    cwd = os.getcwd()
    python = sys.executable

    if sys.platform == "win32":
        # Windows: 用 CREATE_NEW_PROCESS_GROUP + DETACHED_PROCESS
        return _start_windows(python, mode, cwd)
    else:
        # Linux/Mac: 用 shell nohup + 双 fork 脱离
        return _start_unix(python, mode, cwd)


def _start_unix(python: str, mode: str, cwd: str) -> int:
    """Unix/Linux/Mac: 用 nohup + setsid 启动完全独立的后台进程"""

    # 写一个临时启动脚本，确保进程完全脱离
    launcher_script = os.path.join(cwd, "_nb_launcher.sh")

    script_content = f"""#!/bin/bash
cd "{cwd}"
export PYTHONUNBUFFERED=1
export PYTHONPATH="{cwd}"
nohup "{python}" -u -m nb.cli {mode} --loud > "{LOG_FILE}" 2>&1 &
NB_PID=$!
echo $NB_PID > "{PID_FILE}"
# 等一下确认进程启动成功
sleep 2
if kill -0 $NB_PID 2>/dev/null; then
    echo "nb started with PID $NB_PID" >> "{LOG_FILE}"
else
    echo "nb failed to start" >> "{LOG_FILE}"
    rm -f "{PID_FILE}"
fi
"""

    try:
        with open(launcher_script, "w") as f:
            f.write(script_content)
        os.chmod(launcher_script, 0o755)

        # 执行启动脚本（脚本本身会立即返回，nb 在后台运行）
        subprocess.run(
            ["/bin/bash", launcher_script],
            cwd=cwd,
            timeout=10,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        # 清理启动脚本
        try:
            os.remove(launcher_script)
        except Exception:
            pass

        # 等待 PID 文件生成
        for _ in range(10):
            time.sleep(0.5)
            pid = _read_pid_file()
            if pid > 0 and is_process_alive(pid):
                return pid

        # 如果 PID 文件没生成，检查日志
        if os.path.exists(LOG_FILE):
            with open(LOG_FILE, "r") as f:
                content = f.read()
            if content.strip():
                st.code(content[-2000:])

        return 0

    except Exception as e:
        st.error(f"启动失败: {e}")
        try:
            os.remove(launcher_script)
        except Exception:
            pass
        return 0


def _start_windows(python: str, mode: str, cwd: str) -> int:
    """Windows: 用 CREATE_NEW_PROCESS_GROUP 启动"""
    import subprocess

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONPATH"] = cwd

    cmd = [python, "-u", "-m", "nb.cli", mode, "--loud"]

    try:
        log_handle = open(LOG_FILE, "w")

        # Windows 特有标志
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        DETACHED_PROCESS = 0x00000008

        process = subprocess.Popen(
            cmd,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            cwd=cwd,
            env=env,
            creationflags=CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS,
        )

        log_handle.close()

        time.sleep(2)
        if process.poll() is not None:
            with open(LOG_FILE, "r") as f:
                st.code(f.read()[-2000:])
            return 0

        _write_pid_file(process.pid)
        return process.pid

    except Exception as e:
        st.error(f"启动失败: {e}")
        return 0


def termination():
    st.success("进程已终止")
    _remove_pid_file()

    for fname, label in [(LOG_FILE, "当前日志"), (OLD_LOG_FILE, "上次日志")]:
        try:
            with open(fname, "r") as f:
                content = f.read()
            if content.strip():
                st.download_button(
                    f"📥 下载{label}",
                    data=content,
                    file_name=f"nb_{label}.txt",
                    key=f"dl_{label}",
                )
        except FileNotFoundError:
            pass

    CONFIG.pid = 0
    write_config(CONFIG)


# =====================================================================
#  页面主体
# =====================================================================

st.set_page_config(
    page_title="Run",
    page_icon="🏃",
)
hide_st(st)
switch_theme(st, CONFIG)

if check_password(st):

    # ---------- 运行配置 ----------
    with st.expander("Configure Run"):
        CONFIG.show_forwarded_from = st.checkbox(
            "Show 'Forwarded from'", value=CONFIG.show_forwarded_from
        )
        mode = st.radio("Choose mode", ["live", "past"], index=CONFIG.mode)
        if mode == "past":
            CONFIG.mode = 1
            st.warning(
                "Only User Account can be used in Past mode. "
                "Telegram does not allow bot account to go through history of a chat!"
            )
            CONFIG.past.delay = st.slider(
                "Delay in seconds", 0, 100, value=CONFIG.past.delay
            )
        else:
            CONFIG.mode = 0
            CONFIG.live.delete_sync = st.checkbox(
                "Sync when a message is deleted", value=CONFIG.live.delete_sync
            )

        if st.button("Save", key="save_config"):
            write_config(CONFIG)
            st.success("配置已保存")

    # ---------- 进程状态检查（用 PID 文件，不依赖 session） ----------
    pid = get_running_pid()

    # ---------- 启动/停止控制 ----------
    if pid == 0:
        if st.button("▶️ Run", type="primary", key="run_btn"):
            st.info(f"正在启动 nb ({mode} 模式)...")
            new_pid = start_nb_process(mode)
            if new_pid > 0:
                CONFIG.pid = new_pid
                write_config(CONFIG)
                st.success(f"✅ 进程已启动 (PID={new_pid})")
                time.sleep(1)
                rerun()
            else:
                st.error("❌ 启动失败，请检查日志")
    else:
        st.info(f"🟢 nb 正在运行 (PID={pid})")
        st.warning("修改配置后需要先停止再重新启动才能生效")

        if st.button("⏹️ Stop", type="primary", key="stop_btn"):
            with st.spinner("正在停止进程..."):
                success = kill_process(pid)
            if success:
                CONFIG.pid = 0
                write_config(CONFIG)
                termination()
                time.sleep(1)
                rerun()
            else:
                st.error(f"无法终止进程 PID={pid}，请手动处理")
                st.code(f"kill -9 {pid}")

    # ---------- 日志显示 ----------
    st.markdown("---")
    st.markdown("### 📋 Logs")

    if os.path.exists(LOG_FILE):
        lines = st.slider(
            "显示日志行数",
            min_value=50, max_value=2000, value=200, step=50,
            key="log_lines",
        )

        try:
            with open(LOG_FILE, "r") as f:
                all_lines = f.readlines()

            display_lines = all_lines[-lines:] if len(all_lines) > lines else all_lines
            log_content = "".join(display_lines)

            if log_content.strip():
                st.code(log_content)
            else:
                st.info("日志为空，进程可能刚启动")

            st.caption(f"总计 {len(all_lines)} 行，显示最后 {len(display_lines)} 行")

        except Exception as e:
            st.error(f"读取日志失败: {e}")
    else:
        st.info("暂无日志文件")

    if st.button("🔄 刷新日志", key="refresh_logs"):
        rerun()
