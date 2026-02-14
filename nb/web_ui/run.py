import os
import subprocess
import sys
from nb.config import CONFIG

package_dir = os.path.dirname(os.path.abspath(__file__))


def main():
    hello_file = os.path.join(package_dir, "0_👋_Hello.py")
    pages_dir = os.path.join(package_dir, "pages")
    if not os.path.exists(hello_file):
        print(f"主页面不存在: {hello_file}")
        sys.exit(1)
    if not os.path.isdir(pages_dir):
        print(f"pages目录不存在: {pages_dir}")
        sys.exit(1)
    os.environ["STREAMLIT_THEME_BASE"] = CONFIG.theme
    os.environ["STREAMLIT_BROWSER_GATHER_USAGE_STATS"] = "false"
    os.environ["STREAMLIT_SERVER_HEADLESS"] = "true"
    os.environ["STREAMLIT_SERVER_PORT"] = os.getenv("PORT", "8501")
    os.environ["STREAMLIT_SERVER_ADDRESS"] = "0.0.0.0"
    cmd = [sys.executable, "-m", "streamlit", "run", hello_file, "--server.port", os.getenv("PORT", "8501"), "--server.address", "0.0.0.0", "--server.headless", "true"]
    sys.exit(subprocess.call(cmd))


if __name__ == "__main__":
    main()
