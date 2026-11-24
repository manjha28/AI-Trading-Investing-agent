"""One process for the Fly lab app: the read-only order-book recorder, the frozen R3 paper test, and the hourly status report, all writing to the volume.
No database, no keys, no order code."""
from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path

from . import paper, round3_status
from .recorder import Recorder

logger = logging.getLogger(__name__)


async def paper_loop() -> None:
    start, last_status = paper.start_timestamp(), 0.0
    while True:
        try:
            await asyncio.to_thread(paper.run_cycle, start)
            if time.time() - last_status > 3600:
                await asyncio.to_thread(round3_status.write_all)
                last_status = time.time()
        except Exception as error:  # noqa: BLE001 - keep running; a stall shows in the heartbeat age
            logger.warning("paper cycle failed error_class=%s", type(error).__name__)
        await asyncio.sleep(60)


async def main() -> None:
    book = Path(os.environ.get("DELTA_BOOK_DIR", "/data/delta_book"))
    await asyncio.gather(Recorder(["BTCUSD", "ETHUSD"], book).run(), paper_loop())


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)      # one line per candle request would drown the log
    asyncio.run(main())
