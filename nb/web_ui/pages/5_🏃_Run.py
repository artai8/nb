# nb/web_ui/pages/5_🏃_Run.py

import os
import signal
import subprocess
import sys
import time

import streamlit as st

from nb.config import read_config, write_config
from nb.web_ui.password import check_password
from nb.web_ui.utils import hide_st, switch_theme

CONFIG = read_config()


def create_divider():
    """创建分隔线（兼容旧版本 Streamlit）"""
    st.markdown("---")


def get_nb_command(mode: str, loud: bool = True) -> list:
    """
    获取运行 nb 的命令列表。
    优先使用 python -m 方式，确保在任何环境下都能运行。
    """
    args = [mode]
    if loud:
        args.append("--loud")
    
    # 方式 1：使用 python -m 运行模块（最可靠）
    return [sys.executable, "-m", "nb.cli"] + args


def is_process_running(pid: int) -> bool:
    """检查进程是否在运行"""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)  # 发送信号 0 只检查进程是否存在
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # 进程存在但没有权限
        return True
    except Exception:
        return False


def kill_process(pid: int) -> bool:
    """安全地终止进程"""
    if not is_process_running(pid):
        return True
    
    try:
        # 首先尝试优雅终止
        os.kill(pid, signal.SIGTERM)
        time.sleep(2)
        
        # 如果还在运行，强制终止
        if is_process_running(pid):
            os.kill(pid, signal.SIGKILL)
            time.sleep(1)
        
        return not is_process_running(pid)
    except ProcessLookupError:
        return True
    except Exception as e:
        st.error(f"终止进程失败: {e}")
        return False


def termination():
    """进程终止后的清理工作"""
    st.code("进程已终止!")
    
    # 保存旧日志
    try:
        if os.path.exists("logs.txt"):
            if os.path.exists("old_logs.txt"):
                os.remove("old_logs.txt")
            os.rename("logs.txt", "old_logs.txt")
            
            with open("old_logs.txt", "r", encoding="utf-8", errors="ignore") as f:
                log_content = f.read()
                st.download_button(
                    "📥 下载上次日志",
                    data=log_content,
                    file_name="nb_logs.txt",
                    mime="text/plain"
                )
    except Exception as e:
        st.warning(f"保存日志失败: {e}")

    # 重置 PID
    config = read_config()
    config.pid = 0
    write_config(config)
    
    st.button("🔄 刷新页面")


def rerun():
    """兼容不同版本的 Streamlit rerun"""
    if hasattr(st, 'rerun'):
        st.rerun()
    elif hasattr(st, 'experimental_rerun'):
        st.experimental_rerun()
    else:
        st.warning("请手动刷新页面")


# ==================== 页面配置 ====================

st.set_page_config(
    page_title="Run",
    page_icon="🏃",
)

hide_st(st)
switch_theme(st, CONFIG)

if check_password(st):
    
    # ==================== 运行配置 ====================
    with st.expander("⚙️ 运行配置", expanded=False):
        CONFIG.show_forwarded_from = st.checkbox(
            "显示 'Forwarded from'（转发来源）",
            value=CONFIG.show_forwarded_from,
            help="启用后会直接转发消息，保留原始转发标记"
        )
        
        mode = st.radio(
            "选择运行模式",
            ["live", "past"],
            index=CONFIG.mode,
            horizontal=True,
            help="Live: 实时转发新消息 | Past: 转发历史消息"
        )
        
        if mode == "past":
            CONFIG.mode = 1
            st.warning(
                "⚠️ Past 模式仅支持用户账号！Telegram 不允许 Bot 读取聊天历史。"
            )
            CONFIG.past.delay = st.slider(
                "发送延迟（秒）",
                min_value=0,
                max_value=100,
                value=CONFIG.past.delay,
                help="每条消息发送后等待的秒数，建议设置 60+ 以避免限流"
            )
        else:
            CONFIG.mode = 0
            CONFIG.live.delete_sync = st.checkbox(
                "同步删除消息",
                value=CONFIG.live.delete_sync,
                help="当源消息被删除时，同时删除转发的消息"
            )

        if st.button("💾 保存配置"):
            write_config(CONFIG)
            st.success("配置已保存！")

    # 使用 markdown 分隔线代替 st.divider()
    create_divider()

    # ==================== 运行控制 ====================
    
    # 重新读取配置以获取最新 PID
    CONFIG = read_config()
    
    # 检查进程实际状态
    process_running = is_process_running(CONFIG.pid)
    
    # 如果 PID 存在但进程已停止，重置 PID
    if CONFIG.pid != 0 and not process_running:
        st.info("检测到上次进程已停止，正在重置状态...")
        CONFIG.pid = 0
        write_config(CONFIG)
        time.sleep(0.5)
        rerun()

    # ========== 进程未运行状态 ==========
    if CONFIG.pid == 0:
        st.info(f"📋 当前模式: **{mode.upper()}**")
        
        col1, col2 = st.columns([1, 3])
        with col1:
            # use_container_width 在旧版本可能不支持，使用 try-except
            try:
                check = st.button("▶️ 启动", type="primary", use_container_width=True)
            except:
                check = st.button("▶️ 启动", type="primary")
        
        if check:
            # 创建日志文件
            try:
                with open("logs.txt", "w", encoding="utf-8") as logs:
                    logs.write(f"=== nb {mode} 模式启动 ===\n")
                    logs.write(f"启动时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
                    logs.write(f"Python: {sys.executable}\n")
                    logs.write("=" * 40 + "\n\n")
                
                # 获取启动命令
                cmd = get_nb_command(mode, loud=True)
                st.info(f"🚀 启动命令: `{' '.join(cmd)}`")
                
                # 启动进程
                with open("logs.txt", "a", encoding="utf-8") as logs:
                    process = subprocess.Popen(
                        cmd,
                        stdout=logs,
                        stderr=subprocess.STDOUT,
                        env={**os.environ, "PYTHONPATH": "/app", "NB_MODE": mode},
                        cwd="/app" if os.path.exists("/app") else os.getcwd(),
                    )
                
                # 保存 PID
                CONFIG.pid = process.pid
                write_config(CONFIG)
                
                st.success(f"✅ 进程已启动！PID: {process.pid}")
                time.sleep(2)
                rerun()
                
            except FileNotFoundError as e:
                st.error(f"❌ 命令未找到: {e}")
                st.code(f"尝试的命令: {' '.join(cmd)}")
                st.info("💡 请确保 nb 包已正确安装")
            except PermissionError as e:
                st.error(f"❌ 权限不足: {e}")
            except Exception as e:
                st.error(f"❌ 启动失败: {e}")
                import traceback
                st.code(traceback.format_exc())

    # ========== 进程运行中状态 ==========
    else:
        st.success(f"🟢 进程运行中 | PID: {CONFIG.pid} | 模式: {mode.upper()}")
        st.warning("⚠️ 修改配置后需要停止并重新启动才能生效")
        
        col1, col2, col3 = st.columns([1, 1, 2])
        
        with col1:
            try:
                if st.button("⏹️ 停止", type="primary", use_container_width=True):
                    with st.spinner("正在停止进程..."):
                        if kill_process(CONFIG.pid):
                            termination()
                        else:
                            st.error("无法终止进程，请手动处理")
                            st.code(f"sudo kill -9 {CONFIG.pid}")
            except:
                # 旧版本没有 use_container_width
                if st.button("⏹️ 停止", type="primary"):
                    with st.spinner("正在停止进程..."):
                        if kill_process(CONFIG.pid):
                            termination()
                        else:
                            st.error("无法终止进程，请手动处理")
                            st.code(f"sudo kill -9 {CONFIG.pid}")
        
        with col2:
            try:
                if st.button("🔄 重启", use_container_width=True):
                    with st.spinner("正在重启..."):
                        if kill_process(CONFIG.pid):
                            CONFIG.pid = 0
                            write_config(CONFIG)
                            time.sleep(1)
                            rerun()
            except:
                if st.button("🔄 重启"):
                    with st.spinner("正在重启..."):
                        if kill_process(CONFIG.pid):
                            CONFIG.pid = 0
                            write_config(CONFIG)
                            time.sleep(1)
                            rerun()

    # 使用 markdown 分隔线代替 st.divider()
    create_divider()

    # ==================== 日志显示 ====================
    st.subheader("📜 运行日志")
    
    lines = st.slider(
        "显示行数",
        min_value=50,
        max_value=2000,
        value=200,
        step=50
    )
    
    log_container = st.empty()
    
    try:
        if os.path.exists("logs.txt"):
            # 读取最后 N 行
            with open("logs.txt", "r", encoding="utf-8", errors="ignore") as f:
                all_lines = f.readlines()
                display_lines = all_lines[-lines:] if len(all_lines) > lines else all_lines
                log_content = "".join(display_lines)
            
            if log_content.strip():
                log_container.code(log_content, language="log")
            else:
                log_container.info("日志为空，等待输出...")
        else:
            log_container.info("📭 暂无日志文件")
            
    except Exception as e:
        log_container.error(f"读取日志失败: {e}")
    
    # 刷新按钮
    col1, col2 = st.columns([1, 3])
    with col1:
        try:
            if st.button("🔄 刷新日志", use_container_width=True):
                rerun()
        except:
            if st.button("🔄 刷新日志"):
                rerun()
    
    # 自动刷新选项
    with col2:
        auto_refresh = st.checkbox("自动刷新（每 5 秒）", value=False)
        if auto_refresh and CONFIG.pid != 0:
            time.sleep(5)
            rerun()
