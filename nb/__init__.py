"""Package nb: Multi-tenant Telegram message forwarding control plane and runtime."""

try:
    from importlib.metadata import version
    __version__ = version(__package__)
except Exception:
    __version__ = "3.4.0"
