"""Single-tenant past forwarding compatibility wrapper."""

import logging
from nb.multitenant.worker import async_main


async def forward_job() -> None:
    logging.info("Starting past history forwarding job...")
    await async_main()
