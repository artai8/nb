import os
import importlib.resources

import nb.web_ui as wu
from nb.config import CONFIG

try:
    # Python 3.9+
    _ref = importlib.resources.files(wu)
    package_dir = str(_ref)
except AttributeError:
    # Python 3.8 及以下回退
    package_dir = str(importlib.resources.path(package=wu, resource="").__enter__())


def main():
    print(package_dir)
    path = os.path.join(package_dir, "0_👋_Hello.py")
    os.environ["STREAMLIT_THEME_BASE"] = CONFIG.theme
    os.environ["STREAMLIT_BROWSER_GATHER_USAGE_STATS"] = "false"
    os.environ["STREAMLIT_SERVER_HEADLESS"] = "true"
    os.system(f"streamlit run {path}")