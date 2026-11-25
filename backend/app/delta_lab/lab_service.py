"""One process for the Fly lab app: the read-only order-book recorder, the frozen R3 paper test, and the hourly status report, all writing to the volume.
No database, no keys, no order code. A read-only JSON status (GET /status, port 8081) serves the app's R3 card over Fly's private network."""
from __future__ import annotations

import asyncio
import json
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


STATUS_PORT = int(os.environ.get("DELTA_LAB_STATUS_PORT", "8081"))


async def _status(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """GET /status -> the R3 paper status as JSON. Read only, paper data only; reachable on Fly's private network (no public port is configured)."""
    try:
        line = (await asyncio.wait_for(reader.readline(), 5)).decode("latin-1")
        while (await asyncio.wait_for(reader.readline(), 5)) not in (b"\r\n", b"\n", b""):
            pass
        if line.startswith("GET /status "):
            body, code = json.dumps(await asyncio.to_thread(round3_status.status_json), default=str).encode(), "200 OK"
        else:
            body, code = b'{"error":"not found"}', "404 Not Found"
        writer.write(f"HTTP/1.1 {code}\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
        await writer.drain()
    except Exception as error:  # noqa: BLE001 - a bad request must never touch the recorder or the paper loop
        logger.warning("status request failed error_class=%s", type(error).__name__)
    finally:
        writer.close()


async def main() -> None:
    book = Path(os.environ.get("DELTA_BOOK_DIR", "/data/delta_book"))
    server = await asyncio.start_server(_status, host="::", port=STATUS_PORT)
    async with server:
        await asyncio.gather(Recorder(["BTCUSD", "ETHUSD"], book).run(), paper_loop(), server.serve_forever())


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)      # one line per candle request would drown the log
    asyncio.run(main())
