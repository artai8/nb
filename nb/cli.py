import asyncio
from enum import Enum
import logging
from typing import Optional

import typer
from rich.console import Console

from nb import __version__

app = typer.Typer(add_completion=False)
con = Console()


class Mode(str, Enum):
    LIVE = "live"
    PAST = "past"
    SCHEDULE = "schedule"
    WEB = "web"
    API = "api"
    BOT = "bot"
    WORKER = "worker"


def verbosity_callback(value: bool):
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=logging.DEBUG if value else logging.INFO,
    )
    con.print(f"Running nb version {__version__}", style="bold cyan")


def version_callback(value: bool):
    if value:
        con.print(__version__)
        raise typer.Exit()


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
    """Automate custom Telegram message forwarding."""
    if mode == Mode.API:
        from nb.multitenant.api import main as service_main
        service_main()
    elif mode == Mode.BOT:
        from nb.multitenant.control_bot import main as service_main
        service_main()
    elif mode == Mode.WORKER:
        from nb.multitenant.worker import main as service_main
        service_main()
    elif mode == Mode.WEB:
        from nb.web_ui.run import main as service_main
        service_main()
    elif mode == Mode.PAST:
        from nb.past import forward_job
        asyncio.run(forward_job())
    elif mode == Mode.SCHEDULE:
        from nb.schedule import schedule_job
        asyncio.run(schedule_job())
    else:
        from nb.live import start_sync
        asyncio.run(start_sync())


if __name__ == "__main__":
    app()
