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


def rerun():
    """兼容不同版本的 Streamlit rerun"""
    if hasattr(st, 'rerun'):
        st.rerun()
    elif hasattr(st, 'experimental_rerun'):
        st.experimental_rerun()
    else:
        st.warning("Please refresh the page manually.")


def is_process_alive(pid: int) -> bool:
    """跨平台检查进程是否存活"""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)  # 信号 0 不会杀死进程，只检查是否存在
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 进程存在但无权限
    except OSError:
        return False


def kill_process(pid: int) -> bool:
    """安全终止进程"""
    if not is_process_alive(pid):
        return True
    try:
        os.kill(pid, signal.SIGTERM)
        # 等待最多 5 秒
        for _ in range(10):
            time.sleep(0.5)
            if not is_process_alive(pid):
                return True
        # 强制终止
        try:
            os.kill(pid, signal.SIGKILL)
            time.sleep(1)
        except ProcessLookupError:
            pass
        return not is_process_alive(pid)
    except ProcessLookupError:
        return True
    except Exception as e:
        st.error(f"终止进程失败: {e}")
        return False


def start_nb_process(mode: str) -> int:
    """启动 nb 进程，返回 PID。

    关键改进：
    1. 使用 start_new_session=True 使进程脱离父进程组
    2. 正确重定向 stdout/stderr 到日志文件
    3. 设置环境变量确保 Python 输出不缓冲
    """
    log_file = os.path.join(os.getcwd(), "logs.txt")

    # 备份旧日志
    if os.path.exists(log_file):
        old_log = os.path.join(os.getcwd(), "old_logs.txt")
        try:
            os.rename(log_file, old_log)
        except Exception:
            pass

    # 创建新日志文件
    log_fd = open(log_file, "w")

    # 构建环境变量（继承当前环境 + 禁用 Python 缓冲）
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONPATH"] = os.getcwd()

    # ★ 核心修复：用正确的命令启动
    cmd = [
        sys.executable, "-u",  # -u 禁用缓冲
        "-m", "nb.cli",
        mode,
        "--loud",
    ]

    try:
        process = subprocess.Popen(
            cmd,
            stdout=log_fd,
            stderr=subprocess.STDOUT,
            cwd=os.getcwd(),
            env=env,
            start_new_session=True,  # ★ 关键：脱离 Streamlit 进程组
        )

        # 等一小段时间检查进程是否立刻崩溃
        time.sleep(2)
        if process.poll() is not None:
            # 进程已退出，读取错误日志
            log_fd.close()
            with open(log_file, "r") as f:
                error_output = f.read()
            st.error(f"进程启动后立即退出 (code={process.returncode})")
            if error_output.strip():
                st.code(error_output[-2000:])  # 显示最后 2000 字符
            return 0

        log_fd.close()  # 父进程关闭文件描述符，子进程继续持有
        return process.pid

    except Exception as e:
        log_fd.close()
        st.error(f"启动失败: {e}")
        return 0


def termination():
    st.success("进程已终止")
    log_file = os.path.join(os.getcwd(), "logs.txt")
    old_log = os.path.join(os.getcwd(), "old_logs.txt")

    # 提供日志下载
    for fname, label in [(log_file, "当前日志"), (old_log, "上次日志")]:
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

    CONFIG = read_config()
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

    # ---------- 进程状态检查 ----------
    pid = CONFIG.pid

    # 检查记录的 PID 对应的进程是否真的存活
    if pid != 0 and not is_process_alive(pid):
        st.warning(f"记录的进程 (PID={pid}) 已不存在，重置状态")
        CONFIG.pid = 0
        write_config(CONFIG)
        pid = 0

    # ---------- 启动/停止控制 ----------
    if pid == 0:
        # 没有运行中的进程
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
        # 有运行中的进程
        st.info(f"🟢 nb 正在运行 (PID={pid})")
        st.warning(
            "修改配置后需要先停止再重新启动才能生效"
        )

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

    log_file = os.path.join(os.getcwd(), "logs.txt")

    if os.path.exists(log_file):
        lines = st.slider(
            "显示日志行数",
            min_value=50,
            max_value=2000,
            value=200,
            step=50,
            key="log_lines",
        )

        try:
            with open(log_file, "r") as f:
                all_lines = f.readlines()

            # 取最后 N 行
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

    # 手动刷新按钮
    if st.button("🔄 刷新日志", key="refresh_logs"):
        rerun()
