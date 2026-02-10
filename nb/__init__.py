"""Package nb.

The ultimate tool to automate custom telegram message forwarding.
https://github.com/artai8/nb
"""

try:
    from importlib.metadata import version
    __version__ = version(__package__)
except Exception:
    # 如果包没有安装，使用硬编码版本
    __version__ = "1.1.8"  # 从 pyproject.toml 中的版本号
