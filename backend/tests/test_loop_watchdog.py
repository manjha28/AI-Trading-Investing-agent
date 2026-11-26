"""The event-loop watchdog reports a blocked loop and says where it was blocked. Test code only."""
import asyncio
import logging
import time

from app.loop_watchdog import watch_event_loop


def _block_the_loop_for_the_test() -> None:
    time.sleep(0.8)


def test_a_blocked_loop_is_reported_with_the_blocking_frame(caplog):
    async def run() -> None:
        task = asyncio.create_task(watch_event_loop(threshold=0.3, interval=0.05))
        await asyncio.sleep(0.15)
        _block_the_loop_for_the_test()
        await asyncio.sleep(0.2)
        task.cancel()

    with caplog.at_level(logging.WARNING, logger="app.loop_watchdog"):
        asyncio.run(run())
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "event_loop_stalled" in text and "_block_the_loop_for_the_test" in text
    assert "event_loop_recovered" in text
