"""Package nb."""

try:
    from importlib.metadata import version
    __version__ = version(__package__)
except Exception:
    __version__ = "1.1.8"
