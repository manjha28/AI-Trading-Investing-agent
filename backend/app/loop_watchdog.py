"""Reports what the process was doing when the event loop stopped running. A stalled loop cannot answer WebSocket pings, so broker feeds drop with
"keepalive timeout". The stacks logged contain file names, line numbers and source lines only: never local values, so no credential can appear."""
from __future__ import annotations

import asyncio
import logging
import sys
import threading
import time
import traceback

logger = logging.getLogger(__name__)


def _ours(filename: str) -> bool:
    return "site-packages" not in filename and "/usr/local/lib" not in filename and "/usr/lib" not in filename


def _frames(frame, limit: int) -> str:
    """The innermost two frames (what it is waiting on), then this application's own frames (who asked for it)."""
    stack = traceback.extract_stack(frame)
    inner = list(reversed(stack[-2:]))
    ours = [f for f in reversed(stack[:-2]) if _ours(f.filename)][:limit]
    return " <- ".join(f"{f.filename.split('/site-packages/')[-1].split('/app/', 1)[-1]}:{f.lineno} {f.name}" for f in inner + ours)


def snapshot(loop_thread: int) -> str:
    """One line for the loop thread (what is blocking it) and one for every other thread that is inside application code."""
    lines = []
    for ident, frame in sys._current_frames().items():
        if ident == threading.get_ident():
            continue
        if ident == loop_thread:
            lines.insert(0, f"LOOP: {_frames(frame, 12)}")
        elif any("/app/" in f.filename and "site-packages" not in f.filename for f in traceback.extract_stack(frame)):
            lines.append(f"thread: {_frames(frame, 8)}")
    return " || ".join(lines[:6])


async def watch_event_loop(threshold: float = 5.0, interval: float = 0.5, repeat_after: float = 60.0) -> None:
    """Runs until cancelled. A helper thread notices when this coroutine stops ticking."""
    beat = [time.monotonic()]
    stop = threading.Event()
    loop_thread = threading.get_ident()

    def monitor() -> None:
        stalled_since: float | None = None
        last_report = 0.0
        while not stop.wait(interval):
            now = time.monotonic()
            lag = now - beat[0]
            if lag >= threshold:
                if stalled_since is None:
                    stalled_since = beat[0]
                    if now - last_report >= repeat_after or last_report == 0.0:
                        last_report = now
                        logger.warning("event_loop_stalled seconds=%.1f at=%s", lag, snapshot(loop_thread))
            elif stalled_since is not None:
                logger.warning("event_loop_recovered stalled_seconds=%.1f", beat[0] - stalled_since)
                stalled_since = None

    thread = threading.Thread(target=monitor, name="loop-watchdog", daemon=True)
    thread.start()
    try:
        while True:
            beat[0] = time.monotonic()
            await asyncio.sleep(interval)
    finally:
        stop.set()
