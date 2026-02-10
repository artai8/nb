import os
import sys
import subprocess

from nb.config import CONFIG


def _get_package_dir() -> str:
    """获取 web_ui 包的实际文件系统路径"""
    # 方法 1：直接用 __file__ 定位（最可靠）
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

    # 设置环境变量
    os.environ["STREAMLIT_THEME_BASE"] = CONFIG.theme
    os.environ["STREAMLIT_BROWSER_GATHER_USAGE_STATS"] = "false"
    os.environ["STREAMLIT_SERVER_HEADLESS"] = "true"
    os.environ["STREAMLIT_SERVER_PORT"] = os.getenv("PORT", "8501")
    os.environ["STREAMLIT_SERVER_ADDRESS"] = "0.0.0.0"

    # 使用 subprocess 而不是 os.system（避免 shell 解析特殊字符问题）
    cmd = [
        sys.executable, "-m", "streamlit", "run",
        hello_file,
        "--server.port", os.getenv("PORT", "8501"),
        "--server.address", "0.0.0.0",
        "--server.headless", "true",
    ]

    print(f"🚀 启动命令: {' '.join(cmd)}")
    sys.exit(subprocess.call(cmd))


if __name__ == "__main__":
    main()
