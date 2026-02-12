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
PID_FILE = os.path.join(os.getcwd(), "nb_process.pid")


def rerun():
    if hasattr(st, 'rerun'):
        st.rerun()
    elif hasattr(st, 'experimental_rerun'):
        st.experimental_rerun()
    else:
        st.warning("请手动刷新页面。")


def _write_pid_file(pid: int):
    try:
        with open(PID_FILE, "w") as f:
            f.write(str(pid))
    except Exception:
        pass


def _read_pid_file() -> int:
    try:
        if os.path.exists(PID_FILE):
            with open(PID_FILE, "r") as f:
                return int(f.read().strip())
    except Exception:
        pass
    return 0


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


def _get_child_pids(parent_pid: int) -> list:
    children = []
    try:
        if os.path.exists("/proc"):
            for pid_dir in os.listdir("/proc"):
                if not pid_dir.isdigit():
                    continue
                try:
                    with open(f"/proc/{pid_dir}/status", "r") as f:
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
            result = subprocess.run(["pgrep", "-P", str(parent_pid)], capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                for line in result.stdout.strip().split("\n"):
                    line = line.strip()
                    if line.isdigit():
                        children.append(int(line))
        except Exception:
            pass
    return children


def _kill_process_tree(pid: int) -> bool:
    if pid <= 0:
        return True
    all_pids = []

    def _collect_children(parent):
        children = _get_child_pids(parent)
        for child in children:
            all_pids.append(child)
            _collect_children(child)

    _collect_children(pid)
    all_pids.append(pid)
    unique_pids = list(dict.fromkeys(all_pids))
    for p in unique_pids:
        try:
            os.kill(p, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    for _ in range(10):
        time.sleep(0.5)
        if not any(is_process_alive(p) for p in unique_pids):
            return True
    for p in unique_pids:
        if is_process_alive(p):
            try:
                os.kill(p, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
    time.sleep(1)
    still_alive = [p for p in unique_pids if is_process_alive(p)]
    if still_alive:
        try:
            subprocess.run(["pkill", "-9", "-f", "nb.cli"], capture_output=True, timeout=5)
            time.sleep(1)
        except Exception:
            pass
    return not any(is_process_alive(p) for p in unique_pids)


def kill_process(pid: int) -> bool:
    if not is_process_alive(pid):
        _remove_pid_file()
        return True
    success = _kill_process_tree(pid)
    if success:
        _remove_pid_file()
    return success


def _validate_config_for_run(mode: str) -> tuple:
    errors = []
    if CONFIG.login.API_ID == 0:
        errors.append("❌ API_ID 未设置")
    if not CONFIG.login.API_HASH:
        errors.append("❌ API_HASH 未设置")
    if CONFIG.login.user_type == 0:
        if not CONFIG.login.BOT_TOKEN:
            errors.append("❌ Bot Token 未设置")
    else:
        if not CONFIG.login.SESSION_STRING:
            errors.append("❌ Session String 未设置")
    if mode == "past" and CONFIG.login.user_type == 0:
        errors.append("❌ **past 模式不支持 Bot 账号！**\nTelegram 禁止 Bot 遍历聊天历史。\n请切换为 User 账号并填入 Session String。")
    active_forwards = [f for f in CONFIG.forwards if f.use_this]
    if not active_forwards:
        errors.append("❌ 没有启用的转发连接")
    else:
        for f in active_forwards:
            name = f.con_name or "未命名"
            if not f.source and f.source != 0:
                errors.append(f"⚠️ 连接 '{name}' 未设置源")
            if not f.dest:
                errors.append(f"⚠️ 连接 '{name}' 未设置目标")
    return (len(errors) == 0, errors)


def start_nb_process(mode: str) -> int:
    log_file = os.path.join(os.getcwd(), "logs.txt")
    if os.path.exists(log_file):
        old_log = os.path.join(os.getcwd(), "old_logs.txt")
        try:
            os.rename(log_file, old_log)
        except Exception:
            pass
    log_fd = open(log_file, "w")
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONPATH"] = os.getcwd()
    cmd = [sys.executable, "-u", "-m", "nb.cli", mode, "--loud"]
    try:
        process = subprocess.Popen(cmd, stdout=log_fd, stderr=subprocess.STDOUT, cwd=os.getcwd(), env=env)
        time.sleep(2)
        if process.poll() is not None:
            log_fd.close()
            with open(log_file, "r") as f:
                error_output = f.read()
            st.error(f"进程启动后立即退出 (code={process.returncode})")
            if error_output.strip():
                st.code(error_output[-2000:])
            return 0
        log_fd.close()
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
    for fname, label in [(log_file, "当前日志"), (old_log, "上次日志")]:
        try:
            with open(fname, "r") as f:
                content = f.read()
            if content.strip():
                st.download_button(f"📥 下载{label}", data=content, file_name=f"nb_{label}.txt", key=f"dl_{label}")
        except FileNotFoundError:
            pass
    CONFIG = read_config()
    CONFIG.pid = 0
    write_config(CONFIG)
    _remove_pid_file()


st.set_page_config(page_title="运行", page_icon="🏃")
hide_st(st)
switch_theme(st, CONFIG)

if check_password(st):
    with st.expander("运行配置"):
        CONFIG.show_forwarded_from = st.checkbox('显示"转发自"', value=CONFIG.show_forwarded_from)
        mode = st.radio("选择模式", ["live", "past"], index=CONFIG.mode)
        if mode == "past":
            CONFIG.mode = 1
            if CONFIG.login.user_type == 0:
                st.error("🚫 **past 模式不支持 Bot 账号！**\n\nTelegram 禁止 Bot 遍历聊天历史。\n\n**解决方法：**\n1. 前往 **Telegram 登录** 页面\n2. 切换为 **User** 账号\n3. 填入 **Session String**\n4. 保存后返回此页面运行")
            else:
                st.warning("past 模式仅支持 User 账号，Telegram 不允许 Bot 遍历聊天历史。")
            CONFIG.past.delay = st.slider("延迟（秒）", 0, 100, value=CONFIG.past.delay)
        else:
            CONFIG.mode = 0
            CONFIG.live.delete_sync = st.checkbox("同步删除消息", value=CONFIG.live.delete_sync)
        if st.button("保存", key="save_config"):
            write_config(CONFIG)
            st.success("配置已保存")
    pid_from_config = CONFIG.pid
    pid_from_file = _read_pid_file()
    pid = 0
    if pid_from_file > 0 and is_process_alive(pid_from_file):
        pid = pid_from_file
    elif pid_from_config > 0 and is_process_alive(pid_from_config):
        pid = pid_from_config
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
    if pid == 0:
        can_run, validation_errors = _validate_config_for_run(mode)
        if not can_run:
            st.markdown("### ⚠️ 配置问题")
            for err in validation_errors:
                st.error(err)
            st.info("请先解决上述问题再运行。")
        has_critical = any("不支持 Bot" in e for e in validation_errors)
        if st.button("▶️ 运行", type="primary", key="run_btn", disabled=has_critical):
            if not can_run:
                st.error("请先修复配置问题！")
            else:
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
        children = _get_child_pids(pid)
        if children:
            st.caption(f"子进程: {children}")
        st.warning("修改配置后需要先停止再重新启动才能生效")
        col1, col2 = st.columns(2)
        with col1:
            if st.button("⏹️ 停止", type="primary", key="stop_btn"):
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
                    st.code(f"# 手动终止:\nkill -9 {pid}\npkill -9 -f 'nb.cli'")
        with col2:
            if st.button("🔴 强制终止", key="force_kill_btn"):
                with st.spinner("强制终止所有 nb 进程..."):
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except Exception:
                        pass
                    for child_pid in _get_child_pids(pid):
                        try:
                            os.kill(child_pid, signal.SIGKILL)
                        except Exception:
                            pass
                    try:
                        subprocess.run(["pkill", "-9", "-f", "nb.cli"], capture_output=True, timeout=5)
                    except Exception:
                        pass
                    time.sleep(2)
                CONFIG.pid = 0
                write_config(CONFIG)
                _remove_pid_file()
                st.success("已强制终止所有 nb 进程")
                time.sleep(1)
                rerun()
    st.markdown("---")
    st.markdown("### 📋 日志")
    log_file = os.path.join(os.getcwd(), "logs.txt")
    if os.path.exists(log_file):
        lines = st.slider("显示日志行数", min_value=50, max_value=2000, value=200, step=50, key="log_lines")
        try:
            with open(log_file, "r") as f:
                all_lines = f.readlines()
            display_lines = all_lines[-lines:] if len(all_lines) > lines else all_lines
            log_content = "".join(display_lines)
            if log_content.strip():
                st.code(log_content)
            else:
                st.info("日志为空，进程可能刚启动")
            st.caption(f"共 {len(all_lines)} 行，显示最后 {len(display_lines)} 行")
        except Exception as e:
            st.error(f"读取日志失败: {e}")
    else:
        st.info("暂无日志文件")
    if st.button("🔄 刷新日志", key="refresh_logs"):
        rerun()
