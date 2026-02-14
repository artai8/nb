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
    """★ 修复：使用正确的包名检查版本，并处理异常"""
    try:
        from verlat import latest_release
        
        # ★ 修复：使用正确的 PyPI 包名
        # 如果你的包在 PyPI 上的名字不同，请修改这里
        # 或者直接禁用远程版本检查
        PYPI_PACKAGE_NAME = os.getenv("NB_PYPI_NAME", "")
        
        if not PYPI_PACKAGE_NAME:
            # 如果没有设置 PyPI 包名，只显示当前版本
            con.print(f"Running nb version {__version__}", style="bold green")
            return
        
        latver = latest_release(PYPI_PACKAGE_NAME).version
        if __version__ != latver:
            con.print(
                f"nb has a newer release {latver} available!\n"
                "Visit http://bit.ly/update-nb",
                style="bold yellow",
            )
        else:
            con.print(f"Running latest nb version {__version__}", style="bold green")
    except Exception as e:
        # ★ 修复：版本检查失败不应影响正常运行
        con.print(f"Running nb version {__version__}", style="bold green")
        logging.debug(f"版本检查跳过: {e}")


def _pre_check_config(mode: Mode):
    from nb.config import CONFIG, _looks_like_bot_token

    errors = []
    warnings = []
    login = CONFIG.login
    login_type = "User" if login.user_type == 1 else "Bot"

    # API 凭证检查
    if login.API_ID == 0:
        errors.append("API_ID 未设置")
    if not login.API_HASH:
        errors.append("API_HASH 未设置")

    # 模式相关检查
    if mode == Mode.PAST:
        if login.user_type == 0:
            errors.append("❌ past 模式不支持 Bot 账号！Telegram 禁止 Bot 遍历聊天历史。")
        elif not login.SESSION_STRING:
            errors.append("Session String 未设置！")
        elif _looks_like_bot_token(login.SESSION_STRING):
            errors.append("SESSION_STRING 字段中的值是 Bot Token，不是 Session String！")

    if mode == Mode.LIVE:
        if login.user_type == 0:
            if not login.BOT_TOKEN:
                errors.append("Bot Token 未设置")
        else:
            if not login.SESSION_STRING:
                errors.append("Session String 未设置")

    # 连接检查
    active = [f for f in CONFIG.forwards if f.use_this]
    if not active:
        errors.append("没有启用的转发连接")
    else:
        for i, forward in enumerate(active):
            name = forward.con_name or f"连接{i+1}"
            
            # 检查源
            if not forward.source and forward.source != 0:
                errors.append(f"'{name}' 未设置源")
            
            # 检查目标
            if not forward.dest:
                errors.append(f"'{name}' 未设置目标")
            
            # ★ 检查目标 ID 格式
            for dest in forward.dest:
                if isinstance(dest, int) and dest > 0:
                    warnings.append(
                        f"'{name}' 目标 {dest} 是正数，"
                        f"频道/超级群组 ID 通常是负数（如 -100{dest}）"
                    )
            
            # 评论区配置检查
            if forward.comments.enabled:
                if forward.comments.dest_mode == "discussion":
                    if not forward.comments.dest_discussion_groups:
                        warnings.append(f"'{name}' 启用了评论区但未设置目标讨论组")

    # 输出结果
    if errors:
        con.print("\n❌ 配置预检查失败！\n", style="bold red")
        for i, err in enumerate(errors, 1):
            con.print(f"  {i}. {err}", style="red")
        con.print()
        sys.exit(1)

    con.print("✅ 配置预检查通过", style="bold green")
    con.print(f"   模式: {mode.value}", style="dim")
    con.print(f"   登录: {login_type}", style="dim")
    con.print(f"   连接: {len(active)} 个启用", style="dim")
    
    if warnings:
        con.print("\n⚠️  警告:", style="yellow")
        for w in warnings:
            con.print(f"   • {w}", style="yellow")
    con.print()


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
