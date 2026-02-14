# nb/web_ui/run.py

import os
import sys
import subprocess

from nb.config import CONFIG


def _get_package_dir() -> str:
    """获取 web_ui 包的实际文件系统路径"""
    return os.path.dirname(os.path.abspath(__file__))


package_dir = _get_package_dir()


def main():
    hello_file = os.path.join(package_dir, "0_👋_Hello.py")
    pages_dir = os.path.join(package_dir, "pages")

    # 验证文件存在
    if not os.path.exists(hello_file):
        print(f"❌ 主页面不存在: {hello_file}")
        print(f"目录内容: {os.listdir(package_dir)}")
        sys.exit(1)

    if not os.path.isdir(pages_dir):
        print(f"❌ pages 目录不存在: {pages_dir}")
        print(f"目录内容: {os.listdir(package_dir)}")
        sys.exit(1)

    print(f"📂 package_dir: {package_dir}")
    print(f"📄 主页面: {hello_file}")
    print(f"📁 pages 目录: {os.listdir(pages_dir)}")

    # ==================== 核心逻辑：自动适配端口 ====================
    # 1. 优先读取环境变量 PORT（HuggingFace 会自动注入 PORT=7860）
    # 2. 如果没有环境变量，则默认使用 8501（本地运行）
    port = os.getenv("PORT", "8501")
    
    print(f"🔌 Detecting PORT environment variable: {port}")
    print(f"🚀 Starting Streamlit on port: {port}")

    # 设置 Streamlit 环境变量
    os.environ["STREAMLIT_THEME_BASE"] = CONFIG.theme
    os.environ["STREAMLIT_BROWSER_GATHER_USAGE_STATS"] = "false"
    os.environ["STREAMLIT_SERVER_HEADLESS"] = "true"
    os.environ["STREAMLIT_SERVER_PORT"] = port
    os.environ["STREAMLIT_SERVER_ADDRESS"] = "0.0.0.0"

    # 构建启动命令
    cmd = [
        sys.executable, "-m", "streamlit", "run",
        hello_file,
        "--server.port", port,
        "--server.address", "0.0.0.0",
        "--server.headless", "true",
    ]

    print(f"▶️ Executing command: {' '.join(cmd)}")
    
    # 启动进程
    try:
        sys.exit(subprocess.call(cmd))
    except KeyboardInterrupt:
        print("\n🛑 Streamlit server stopped by user.")
        sys.exit(0)


if __name__ == "__main__":
    main()
