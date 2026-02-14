# nb/web_ui/pages/5_🏃_Run.py

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
    with open(PID_FILE, "w") as f:
        f.write(str(pid))


def _remove_pid_file():
    try:
        if os.path.exists(PID_FILE):
            os.remove(PID_FILE)
    except Exception:
        pass


def is_process_alive(pid: int) -> bool:
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
    """从 PID 文件和 CONFIG 双重检查，返回实际存活的进程 PID"""
    file_pid = _read_pid_file()
    config_pid = CONFIG.pid

    # 优先 PID 文件
    if file_pid > 0 and is_process_alive(file_pid):
        if config_pid != file_pid:
            CONFIG.pid = file_pid
            write_config(CONFIG)
        return file_pid

    # 再看 CONFIG
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


def _kill_process_tree(pid: int) -> bool:
    """杀掉进程及其所有子进程"""
    killed = False

    # 方法1: 用 pkill 杀整个进程组
    try:
        # 获取进程组 ID
        pgid = os.getpgid(pid)
        os.killpg(pgid, signal.SIGTERM)
        time.sleep(2)
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        killed = True
    except (ProcessLookupError, PermissionError, OSError):
        pass

    # 方法2: 直接杀 PID
    if is_process_alive(pid):
        try:
            os.kill(pid, signal.SIGTERM)
            time.sleep(2)
        except ProcessLookupError:
            killed = True
        except Exception:
            pass

    if is_process_alive(pid):
        try:
            os.kill(pid, signal.SIGKILL)
            time.sleep(1)
        except ProcessLookupError:
            killed = True
        except Exception:
            pass

    # 方法3: 用系统命令强杀（兜底）
    if is_process_alive(pid):
        try:
            os.system(f"kill -9 {pid} 2>/dev/null")
            time.sleep(1)
        except Exception:
            pass

    # 方法4: 杀掉所有 nb.cli 相关进程（最后手段）
    if is_process_alive(pid):
        try:
            os.system("pkill -9 -f 'nb.cli' 2>/dev/null")
            time.sleep(1)
        except Exception:
            pass

    return not is_process_alive(pid)


def kill_process(pid: int) -> bool:
    """安全终止进程"""
    if not is_process_alive(pid):
        _remove_pid_file()
        return True

    success = _kill_process_tree(pid)
    _remove_pid_file()
    return success


def start_nb_process(mode: str) -> int:
    """启动 nb 进程"""
    # 备份旧日志
    if os.path.exists(LOG_FILE):
        try:
            os.rename(LOG_FILE, OLD_LOG_FILE)
        except Exception:
            pass

    cwd = os.getcwd()
    python = sys.executable
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONPATH"] = cwd

    cmd = [python, "-u", "-m", "nb.cli", mode, "--loud"]

    try:
        # 用 os.open 获取持久的文件描述符
        log_fd = os.open(LOG_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)

        process = subprocess.Popen(
            cmd,
            stdout=log_fd,
            stderr=log_fd,
            stdin=subprocess.DEVNULL,
            cwd=cwd,
            env=env,
            start_new_session=True,  # 创建新的进程组
        )

        # 父进程关闭自己的 fd 副本
        os.close(log_fd)

        # 等一下检查是否立刻崩溃
        time.sleep(2)
        if process.poll() is not None:
            try:
                with open(LOG_FILE, "r") as f:
                    error_output = f.read()
                st.error(f"进程启动后立即退出 (code={process.returncode})")
                if error_output.strip():
                    st.code(error_output[-2000:])
            except Exception:
                pass
            return 0

        pid = process.pid
        _write_pid_file(pid)
        return pid

    except Exception as e:
        try:
            os.close(log_fd)
        except Exception:
            pass
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

    # 进程状态检查
    pid = get_running_pid()

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
                st.error(f"无法终止进程 PID={pid}")
                st.code(f"# 手动终止命令：\nkill -9 {pid}\npkill -9 -f 'nb.cli'")
                # 提供强制清理按钮
                if st.button("🔴 强制清理状态", key="force_clean"):
                    os.system(f"kill -9 {pid} 2>/dev/null")
                    os.system("pkill -9 -f 'nb.cli' 2>/dev/null")
                    CONFIG.pid = 0
                    write_config(CONFIG)
                    _remove_pid_file()
                    time.sleep(2)
                    rerun()

    # 日志显示
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
