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

# ★ 全局 PID 文件路径（比配置文件更可靠）
PID_FILE = os.path.join(os.getcwd(), "nb_process.pid")


def rerun():
    """兼容不同版本的 Streamlit rerun"""
    if hasattr(st, 'rerun'):
        st.rerun()
    elif hasattr(st, 'experimental_rerun'):
        st.experimental_rerun()
    else:
        st.warning("Please refresh the page manually.")


def _write_pid_file(pid: int):
    """写入 PID 文件"""
    try:
        with open(PID_FILE, "w") as f:
            f.write(str(pid))
    except Exception as e:
        st.warning(f"写入 PID 文件失败: {e}")


def _read_pid_file() -> int:
    """读取 PID 文件"""
    try:
        if os.path.exists(PID_FILE):
            with open(PID_FILE, "r") as f:
                return int(f.read().strip())
    except Exception:
        pass
    return 0


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
        os.kill(pid, 0)  # 信号 0 不会杀死进程，只检查是否存在
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 进程存在但无权限
    except OSError:
        return False


def _get_child_pids(parent_pid: int) -> list:
    """获取进程的所有子进程 PID"""
    children = []
    try:
        # 方法 1: 使用 /proc (Linux)
        if os.path.exists("/proc"):
            for pid_dir in os.listdir("/proc"):
                if not pid_dir.isdigit():
                    continue
                try:
                    status_file = f"/proc/{pid_dir}/status"
                    with open(status_file, "r") as f:
                        for line in f:
                            if line.startswith("PPid:"):
                                ppid = int(line.split(":")[1].strip())
                                if ppid == parent_pid:
                                    children.append(int(pid_dir))
                                break
                except (FileNotFoundError, PermissionError, ValueError):
                    continue
    except Exception:
        pass

    if not children:
        try:
            # 方法 2: 使用 pgrep 命令
            result = subprocess.run(
                ["pgrep", "-P", str(parent_pid)],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                for line in result.stdout.strip().split("\n"):
                    line = line.strip()
                    if line.isdigit():
                        children.append(int(line))
        except Exception:
            pass

    return children


def _kill_process_tree(pid: int) -> bool:
    """杀死进程及其所有子进程（递归）"""
    if pid <= 0:
        return True

    # 1. 先收集所有子进程（递归）
    all_pids = []

    def _collect_children(parent):
        children = _get_child_pids(parent)
        for child in children:
            all_pids.append(child)
            _collect_children(child)

    _collect_children(pid)
    all_pids.append(pid)  # 父进程放最后

    # 去重，保持顺序（子进程在前，父进程在后）
    seen = set()
    unique_pids = []
    for p in all_pids:
        if p not in seen:
            seen.add(p)
            unique_pids.append(p)

    # 2. 先发 SIGTERM 给所有进程
    for p in unique_pids:
        try:
            os.kill(p, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pass

    # 3. 等待最多 5 秒
    for _ in range(10):
        time.sleep(0.5)
        alive = [p for p in unique_pids if is_process_alive(p)]
        if not alive:
            return True

    # 4. 还活着的用 SIGKILL 强制终止
    alive = [p for p in unique_pids if is_process_alive(p)]
    for p in alive:
        try:
            os.kill(p, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass

    time.sleep(1)

    # 5. 最终检查
    still_alive = [p for p in unique_pids if is_process_alive(p)]
    if still_alive:
        # 最后手段：用 pkill 杀掉包含 nb.cli 的 Python 进程
        try:
            subprocess.run(
                ["pkill", "-9", "-f", "nb.cli"],
                capture_output=True, timeout=5
            )
            time.sleep(1)
        except Exception:
            pass

    return not any(is_process_alive(p) for p in unique_pids)


def kill_process(pid: int) -> bool:
    """安全终止进程（含子进程树）"""
    if not is_process_alive(pid):
        _remove_pid_file()
        return True

    success = _kill_process_tree(pid)
    if success:
        _remove_pid_file()
    return success


def start_nb_process(mode: str) -> int:
    """启动 nb 进程，返回 PID。"""
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
            # ★ 不使用 start_new_session，这样可以直接通过 PID 管理
            # 改为用 PID 文件 + 进程树杀死来管理
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

        # ★ 写入 PID 文件
        _write_pid_file(process.pid)

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
    _remove_pid_file()


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
    # ★ 同时从配置文件和 PID 文件获取 PID，取存活的那个
    pid_from_config = CONFIG.pid
    pid_from_file = _read_pid_file()

    # 优先使用 PID 文件中的值
    pid = 0
    if pid_from_file > 0 and is_process_alive(pid_from_file):
        pid = pid_from_file
    elif pid_from_config > 0 and is_process_alive(pid_from_config):
        pid = pid_from_config

    # 同步状态
    if pid == 0:
        if CONFIG.pid != 0:
            CONFIG.pid = 0
            write_config(CONFIG)
        _remove_pid_file()
    else:
        if CONFIG.pid != pid:
            CONFIG.pid = pid
            write_config(CONFIG)
        _write_pid_file(pid)

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

        # ★ 显示子进程信息
        children = _get_child_pids(pid)
        if children:
            st.caption(f"子进程: {children}")

        st.warning(
            "修改配置后需要先停止再重新启动才能生效"
        )

        col1, col2 = st.columns(2)

        with col1:
            if st.button("⏹️ Stop", type="primary", key="stop_btn"):
                with st.spinner("正在停止进程树..."):
                    success = kill_process(pid)
                if success:
                    CONFIG.pid = 0
                    write_config(CONFIG)
                    termination()
                    time.sleep(1)
                    rerun()
                else:
                    st.error(f"无法终止进程 PID={pid}")
                    st.code(
                        f"# 手动终止命令:\n"
                        f"kill -9 {pid}\n"
                        f"pkill -9 -f 'nb.cli'"
                    )

        with col2:
            if st.button("🔴 Force Kill", key="force_kill_btn"):
                with st.spinner("强制终止所有 nb 进程..."):
                    # 强制杀死所有相关进程
                    killed = False
                    try:
                        # 杀主进程
                        os.kill(pid, signal.SIGKILL)
                        killed = True
                    except Exception:
                        pass

                    # 杀所有子进程
                    for child_pid in _get_child_pids(pid):
                        try:
                            os.kill(child_pid, signal.SIGKILL)
                        except Exception:
                            pass

                    # 用 pkill 清理残留
                    try:
                        subprocess.run(
                            ["pkill", "-9", "-f", "nb.cli"],
                            capture_output=True, timeout=5
                        )
                    except Exception:
                        pass

                    time.sleep(2)

                CONFIG.pid = 0
                write_config(CONFIG)
                _remove_pid_file()
                st.success("已强制终止所有 nb 进程")
                time.sleep(1)
                rerun()

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
