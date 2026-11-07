"""Single-tenant live forwarding compatibility wrapper."""

import logging
from nb.multitenant.worker import async_main


async def start_sync() -> None:
    logging.info("Starting live forwarding engine via multi-tenant worker runtime...")
    await async_main()
