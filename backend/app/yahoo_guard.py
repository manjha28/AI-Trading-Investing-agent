"""Shared circuit breaker for Yahoo Finance (fundamentals, valuation inputs, news).

Yahoo blocks or rate-limits server traffic ("401 Invalid Crumb"). Hundreds of stocks times several calls would otherwise hammer it and flood the logs, so
after a run of consecutive failures every Yahoo caller pauses for a cooldown. Callers treat a paused Yahoo as "source unavailable" (never as zero or neutral).
"""
from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger(__name__)
FAILURES_TO_TRIP = 5
COOLDOWN_SECONDS = 6 * 3600
_lock = threading.Lock()
_consecutive = 0
_blocked_until = 0.0


def allowed() -> bool:
    with _lock:
        return time.monotonic() >= _blocked_until


def seconds_left() -> int:
    with _lock:
        return max(0, int(_blocked_until - time.monotonic()))


def record(ok: bool) -> None:
    global _consecutive, _blocked_until
    with _lock:
        if ok:
            _consecutive = 0
            return
        _consecutive += 1
        if _consecutive >= FAILURES_TO_TRIP and time.monotonic() >= _blocked_until:
            _blocked_until = time.monotonic() + COOLDOWN_SECONDS
            _consecutive = 0
            logger.warning("yahoo_paused after %d consecutive failures; no Yahoo calls for %d hours", FAILURES_TO_TRIP, COOLDOWN_SECONDS // 3600)


def reset() -> None:
    global _consecutive, _blocked_until
    with _lock:
        _consecutive, _blocked_until = 0, 0.0


def trip() -> None:
    """Pause immediately (used when a bulk request shows Yahoo is refusing nearly everything)."""
    global _blocked_until
    with _lock:
        if time.monotonic() >= _blocked_until:
            logger.warning("yahoo_paused: bulk request failed for almost every symbol; no Yahoo calls for %d hours", COOLDOWN_SECONDS // 3600)
        _blocked_until = time.monotonic() + COOLDOWN_SECONDS
