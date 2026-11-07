"""Single-tenant scheduled forwarding compatibility wrapper."""

import logging
from nb.multitenant.worker import async_main


async def schedule_job() -> None:
    logging.info("Starting scheduled forwarding job...")
    await async_main()
