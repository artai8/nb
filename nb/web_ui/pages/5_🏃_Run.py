def start_nb_process(mode: str) -> int:
    log_file = os.path.join(os.getcwd(), "logs.txt")

    # ===== 修复：启动前清理残留进程和文件 =====
    try:
        subprocess.run(
            ["pkill", "-f", "nb.cli"],
            capture_output=True, timeout=5
        )
        time.sleep(1)
    except Exception:
        pass

    # 清理残留 session 文件
    for item in os.listdir(os.getcwd()):
        if item.endswith(".session") or item.endswith(".session-journal"):
            try:
                os.remove(os.path.join(os.getcwd(), item))
            except Exception:
                pass

    _remove_pid_file()
    # ===== 清理结束 =====

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
        process = subprocess.Popen(
            cmd, stdout=log_fd, stderr=subprocess.STDOUT,
            cwd=os.getcwd(), env=env
        )
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
