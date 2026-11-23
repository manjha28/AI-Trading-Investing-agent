"""Keep the Dhan instrument master (a ~230,000-row download) warm so no page load ever waits for it.

Warms at start-up, then refreshes in the background well before the 24h cache expires. The old copy keeps serving until the new one is
swapped in, and a failed refresh simply leaves the old copy in place.
"""
from __future__ import annotations

import asyncio
import logging
import time

from app.broker_adapters import DhanAdapter

logger = logging.getLogger("dhan_master_warm")
REFRESH_AFTER_SECONDS = 20 * 3600
CHECK_EVERY_SECONDS = 3600


def warm_once() -> str:
    """Download the master if there is none or it is older than 20h, and build its lookup index. Returns what it did."""
    cache = DhanAdapter._scrip_master_cache
    age = None if cache is None else time.monotonic() - cache[0]
    if age is not None and age < REFRESH_AFTER_SECONDS:
        DhanAdapter._tick_index()  # built once here, so the first live tick of an instrument never pays for it
        return "fresh"
    try:
        DhanAdapter._scrip_master(force=True)
        DhanAdapter._equity_lookup()
        DhanAdapter._tick_index()
        return "refreshed"
    except Exception:  # noqa: BLE001 - the previous copy (if any) keeps serving
        logger.warning("dhan_master_warm_failed", exc_info=True)
        return "failed"


async def dhan_master_warm_loop() -> None:
    while True:
        try:
            await asyncio.to_thread(warm_once)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("dhan_master_warm_loop_error")
        await asyncio.sleep(CHECK_EVERY_SECONDS)
