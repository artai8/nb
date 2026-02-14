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
    PAST = "past"
    LIVE = "live"


def verbosity_callback(value: bool):
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


def version_callback(value: bool):
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
    from nb.config import CONFIG, _looks_like_bot_token

    errors = []
    login = CONFIG.login
    login_type = "User" if login.user_type == 1 else "Bot"

    if login.API_ID == 0:
        errors.append("API_ID 未设置")
    if not login.API_HASH:
        errors.append("API_HASH 未设置")

    if mode == Mode.PAST:
        if login.user_type == 0:
            if login.SESSION_STRING and not _looks_like_bot_token(login.SESSION_STRING):
                pass
            else:
                errors.append("past 模式不支持 Bot 账号！")
        if login.user_type == 1:
            if not login.SESSION_STRING:
                errors.append("Session String 未设置！")
            elif _looks_like_bot_token(login.SESSION_STRING):
                errors.append("SESSION_STRING 字段中的值是 Bot Token！")

    if mode == Mode.LIVE:
        if login.user_type == 0 and not login.BOT_TOKEN:
            errors.append("Bot Token 未设置")
        if login.user_type == 1 and not login.SESSION_STRING:
            errors.append("Session String 未设置")

    active = [f for f in CONFIG.forwards if f.use_this]
    if not active:
        errors.append("没有启用的转发连接")

    if errors:
        con.print("\n❌ 配置预检查失败！\n", style="bold red")
        for i, err in enumerate(errors, 1):
            con.print(f"  {i}. {err}\n", style="red")
        sys.exit(1)

    con.print("✅ 配置预检查通过", style="bold green")
    con.print(f"   模式: {mode.value}", style="dim")
    con.print(f"   登录: {login_type}", style="dim")
    con.print(f"   连接: {len(active)} 个启用\n", style="dim")


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
    if FAKE:
        logging.critical(f"You are running fake with {mode} mode")
        sys.exit(1)

    _pre_check_config(mode)

    if mode == Mode.PAST:
        from nb.past import forward_job
        asyncio.run(forward_job())
    else:
        from nb.live import start_sync
        asyncio.run(start_sync())


if __name__ == "__main__":
    app()
