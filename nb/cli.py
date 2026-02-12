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
    """在启动异步任务之前做基本配置检查。

    注意: 此时 config.py 已经执行了 _sync_env_to_config()，
    所以 CONFIG 中的值已经是环境变量覆盖后的最新值。
    """
    from nb.config import CONFIG, _looks_like_bot_token

    errors = []
    login = CONFIG.login
    login_type = "User" if login.user_type == 1 else "Bot"

    con.print(
        f"\n📋 [dim]当前登录配置:[/dim]\n"
        f"   [dim]类型: {login_type}[/dim]\n"
        f"   [dim]API_ID: {'已设置' if login.API_ID else '未设置'}[/dim]\n"
        f"   [dim]API_HASH: {'已设置' if login.API_HASH else '未设置'}[/dim]\n"
        f"   [dim]Session String: {'已设置 (长度={})'.format(len(login.SESSION_STRING)) if login.SESSION_STRING else '未设置'}[/dim]\n"
        f"   [dim]Bot Token: {'已设置' if login.BOT_TOKEN else '未设置'}[/dim]\n"
    )

    # 检查 API 凭证
    if login.API_ID == 0:
        errors.append("API_ID 未设置（请在 .env 或 Web UI 中设置）")
    if not login.API_HASH:
        errors.append("API_HASH 未设置（请在 .env 或 Web UI 中设置）")

    # past 模式必须用 User
    if mode == Mode.PAST:
        if login.user_type == 0:
            # Bot 模式但可能有 SESSION_STRING（环境变量设置了但 user_type 没同步）
            if login.SESSION_STRING and not _looks_like_bot_token(login.SESSION_STRING):
                con.print(
                    "⚠️ [yellow]user_type=Bot 但检测到有效的 Session String，"
                    "past 模式将尝试使用 Session String[/yellow]"
                )
                # 不报错，get_SESSION 中会自动 fallback
            else:
                errors.append(
                    "past 模式不支持 Bot 账号！\n"
                    "  Telegram 禁止 Bot 使用 GetHistoryRequest。\n"
                    "  请设置环境变量 SESSION_STRING 或在 Web UI 中切换为 User。"
                )

        if login.user_type == 1:
            if not login.SESSION_STRING:
                errors.append(
                    "Session String 未设置！\n"
                    "  请设置环境变量 SESSION_STRING 或在 Web UI 中填入。\n"
                    "  获取: https://replit.com/@artai8/tg-login?v=1"
                )
            elif _looks_like_bot_token(login.SESSION_STRING):
                errors.append(
                    "SESSION_STRING 字段中的值是 Bot Token，不是 Session String！\n"
                    "  Bot Token 格式: 123456789:ABCdef... (短)\n"
                    "  Session String:  1BQANOTEuMT...     (长, 200+字符)\n"
                    "  请检查环境变量 SESSION_STRING 的值。"
                )

    # live 模式检查
    if mode == Mode.LIVE:
        if login.user_type == 0 and not login.BOT_TOKEN:
            errors.append("Bot Token 未设置")
        if login.user_type == 1 and not login.SESSION_STRING:
            errors.append("Session String 未设置")

    # 检查连接
    active = [f for f in CONFIG.forwards if f.use_this]
    if not active:
        errors.append("没有启用的转发连接（请在 Connections 页面添加）")

    if errors:
        con.print("\n❌ [bold red]配置预检查失败！[/bold red]\n")
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
    """The ultimate tool to automate custom telegram message forwarding.

    Source Code: https://github.com/artai8/nb

    For updates join telegram channel @aahniks_code

    To run web interface run `nb-web` command.
    """
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
