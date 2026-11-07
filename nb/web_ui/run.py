"""Launch the Streamlit Web UI."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def main() -> None:
    app_path = Path(__file__).with_name("0_👋_Hello.py")
    port = os.getenv("PORT", "8501")
    command = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(app_path),
        "--server.port",
        port,
        "--server.address",
        "0.0.0.0",
        "--server.headless",
        "true",
        "--browser.gatherUsageStats",
        "false",
    ]
    subprocess.run(command, check=True)


if __name__ == "__main__":
    main()
