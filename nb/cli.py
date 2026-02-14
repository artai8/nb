# nb/cli.py 完整代码

import asyncio
import logging
import os
import sys
from enum import Enum
from typing import Optional
import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.logging import RichHandler

load_dotenv(".env")
con = Console()
app = typer.Typer(add_completion=False)

class Mode(str, Enum):
    PAST = "past"
    LIVE = "live"

def setup_logging(verbose: bool):
    level = logging.INFO if verbose else logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=[RichHandler(rich_tracebacks=True)] if sys.stdout.isatty() else [logging.StreamHandler(sys.stdout)]
    )
    if verbose:
        logging.info("📢 详细日志模式已开启")

@app.command()
def main(
    mode: Mode = typer.Argument(..., help="运行模式: past 或 live"),
    verbose: bool = typer.Option(False, "--loud", "-l", help="显示详细日志"),
):
    setup_logging(verbose)
    if mode == Mode.PAST:
        from nb.past import forward_job
        asyncio.run(forward_job())
    else:
        from nb.live import start_sync
        asyncio.run(start_sync())

if __name__ == "__main__":
    app()
