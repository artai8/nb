"""Shared utility functions for nb."""

import os
import platform
import sys
from typing import Any


def cleanup(*paths: Any) -> None:
    """Remove temporary files safely."""
    for path in paths:
        if not path:
            continue
        try:
            if os.path.exists(str(path)):
                os.remove(str(path))
        except OSError:
            pass


def platform_info() -> str:
    return (
        f"NB Engine\n"
        f"- Python: {sys.version.split()[0]}\n"
        f"- OS: {platform.system()} {platform.release()}\n"
    )
