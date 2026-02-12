"""This module implements the command line interface for nb."""

import asyncio
import logging
import os
import sys
from enum import Enum
from typing import Optional

import typer
from dotenv import load_dotenv
from rich import console, traceback
from rich.logging import RichHandler
from verlat import latest_release

from nb import __version__

load_dotenv(".env")

FAKE = bool(os.getenv("FAKE"))
app = typer.Typer(add_completion=False)

con = console.Console()


def topper():
    print("nb")
    version_check()
    print("\n")


class Mode(str, Enum):
    """nb works in two modes."""

    PAST = "past"
    LIVE = "live"


def verbosity_callback(value: bool):
    """Set logging level."""
    traceback.install()
    if value:
        level = logging.INFO
    else:
        level = logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            RichHandler(
                rich_tracebacks=True,
                markup=True,
            )
        ],
        force=True,
    )
    topper()
    logging.info("Verbosity turned on! This is suitable for debugging")


def version_callback(value: bool):
    """Show current version and exit."""

    if value:
        con.print(__version__)
        raise typer.Exit()


def version_check():
    try:
        latver = latest_release("nb").version
        if __version__ != latver:
            con.print(
                f"nb has a newer release {latver} available!\n"
                "Visit http://bit.ly/update-nb",
                style="bold yellow",
            )
        else:
            con.print(f"Running latest nb version {__version__}", style="bold green")
    except Exception:
        con.print(f"Running nb version {__version__}", style="bold green")


def _pre_check_config(mode: Mode):
    """在启动异步任务之前做基本配置检查。"""
    from nb.config import CONFIG

    # 检查 API 凭证
    if CONFIG.login.API_ID == 0 or CONFIG.login.API_HASH == "":
        con.print(
            "❌ [bold red]API_ID 或 API_HASH 未设置！[/bold red]\n"
            "请在 Web UI → Telegram Login 页面中设置，\n"
            "或在 .env 文件中设置 API_ID 和 API_HASH。\n"
            "获取方法: https://my.telegram.org",
        )
        sys.exit(1)

    # past 模式必须用用户账号
    if mode == Mode.PAST:
        if CONFIG.login.user_type != 1:
            con.print(
                "❌ [bold red]past 模式仅支持用户账号（User Account）！[/bold red]\n\n"
                "Telegram 禁止 Bot 账号遍历聊天历史记录（GetHistoryRequest）。\n\n"
                "[bold yellow]解决方法：[/bold yellow]\n"
                "  1. 打开 Web UI → Telegram Login\n"
                "  2. 将账号类型切换为 [bold]User[/bold]\n"
                "  3. 填入 Session String\n"
                "  4. 保存配置后重新运行\n\n"
                "[dim]获取 Session String: https://replit.com/@artai8/tg-login?v=1[/dim]",
            )
            sys.exit(1)

        if not CONFIG.login.SESSION_STRING:
            con.print(
                "❌ [bold red]用户账号未设置 Session String！[/bold red]\n\n"
                "请在 Web UI → Telegram Login 页面中填入 Session String。\n\n"
                "[dim]获取方法: https://replit.com/@artai8/tg-login?v=1[/dim]",
            )
            sys.exit(1)

    # live 模式检查
    if mode == Mode.LIVE:
        if CONFIG.login.user_type == 0 and not CONFIG.login.BOT_TOKEN:
            con.print(
                "❌ [bold red]Bot 账号未设置 BOT_TOKEN！[/bold red]\n"
                "请在 Web UI → Telegram Login 页面中填入 Bot Token。",
            )
            sys.exit(1)

        if CONFIG.login.user_type == 1 and not CONFIG.login.SESSION_STRING:
            con.print(
                "❌ [bold red]用户账号未设置 Session String！[/bold red]\n"
                "请在 Web UI → Telegram Login 页面中填入 Session String。",
            )
            sys.exit(1)

    # 检查是否有转发连接
    if not CONFIG.forwards or not any(f.use_this for f in CONFIG.forwards):
        con.print(
            "⚠️ [bold yellow]没有启用的转发连接！[/bold yellow]\n"
            "请在 Web UI → Connections 页面中添加并启用至少一个连接。",
        )
        sys.exit(1)

    con.print("✅ 配置预检查通过", style="bold green")


@app.command()
def main(
    mode: Mode = typer.Argument(
        ..., help="Choose the mode in which you want to run nb.", envvar="NB_MODE"
    ),
    verbose: Optional[bool] = typer.Option(
        None,
        "--loud",
        "-l",
        callback=verbosity_callback,
        envvar="LOUD",
        help="Increase output verbosity.",
    ),
    version: Optional[bool] = typer.Option(
        None,
        "--version",
        "-v",
        callback=version_callback,
        help="Show version and exit.",
    ),
):
    """The ultimate tool to automate custom telegram message forwarding.

    Source Code: https://github.com/artai8/nb

    For updates join telegram channel @aahniks_code

    To run web interface run `nb-web` command.
    """
    if FAKE:
        logging.critical(f"You are running fake with {mode} mode")
        sys.exit(1)

    # ★ 启动前预检查配置
    _pre_check_config(mode)

    if mode == Mode.PAST:
        from nb.past import forward_job

        asyncio.run(forward_job())
    else:
        from nb.live import start_sync

        asyncio.run(start_sync())


# ★ 关键：允许 python -m nb.cli live --loud 直接运行
if __name__ == "__main__":
    app()
