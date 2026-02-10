import os
import importlib.resources
import subprocess
import sys

import nb.web_ui as wu
from nb.config import CONFIG

try:
    # Python 3.9+
    _ref = importlib.resources.files(wu)
    package_dir = str(_ref)  # 确保转为字符串
except AttributeError:
    # Python 3.8 及以下
    import pkg_resources
    package_dir = pkg_resources.resource_filename('nb.web_ui', '')

def main():
    # 确保 package_dir 是字符串
    if not isinstance(package_dir, str):
        package_dir_str = str(package_dir)
    else:
        package_dir_str = package_dir
    
    print(f"Package directory: {package_dir_str}")
    
    # 使用绝对路径
    hello_file = os.path.join(package_dir_str, "0_👋_Hello.py")
    
    # 检查文件是否存在
    if not os.path.exists(hello_file):
        # 如果文件不存在，尝试直接路径
        hello_file = "/app/nb/web_ui/0_👋_Hello.py"
    
    # 设置环境变量
    os.environ["STREAMLIT_THEME_BASE"] = CONFIG.theme
    os.environ["STREAMLIT_BROWSER_GATHER_USAGE_STATS"] = "false"
    os.environ["STREAMLIT_SERVER_HEADLESS"] = "true"
    
    # 使用 subprocess 而不是 os.system（更安全）
    cmd = [
        "streamlit", "run", 
        hello_file,
        "--server.port=8501",
        "--server.address=0.0.0.0",
        "--server.headless=true"
    ]
    
    print(f"Running command: {' '.join(cmd)}")
    subprocess.run(cmd)

if __name__ == "__main__":
    main()
