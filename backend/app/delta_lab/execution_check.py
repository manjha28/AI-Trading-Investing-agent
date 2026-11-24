"""Week-4 execution check: for every paper signal, what the RECORDED Delta order book says a taker fill at my size would have cost against the intended entry.
slippage_bps > 0 is worse than intended. Uses the recorder's files (data/delta_book or /data/delta_book). Read-only."""
from __future__ import annotations

import bisect
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from .measure_costs import CONTRACT_VALUE, rows, walk

MAX_AGE_S = 10.0       # the snapshot must be within 10 s of the signal, else no measurement (never a guess)


def _day_books(root: Path, symbol: str, day: str) -> tuple[list[float], list[dict]]:
    f = root / symbol / f"book-{day}.jsonl.gz"
    if not f.exists():
        return [], []
    data = list(rows(f))
    return [r["t"] for r in data], data


def taker_slippage_bps(root: Path, trade: dict) -> float | None:
    """trade: symbol, entry_time (epoch s), side, qty, entry_ref (intended price)."""
    ts = float(trade["entry_time"])
    day = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
    times, data = _day_books(root, trade["symbol"], day)
    if not times:
        return None
    i = bisect.bisect_right(times, ts + 1.0) - 1
    if i < 0 or ts - times[i] > MAX_AGE_S:
        return None
    snap = data[i]
    avg = walk(snap["asks"] if trade["side"] == "long" else snap["bids"], float(trade["qty"]))
    if avg is None:
        return None
    sign = 1.0 if trade["side"] == "long" else -1.0
    return sign * (avg - float(trade["entry_ref"])) / float(trade["entry_ref"]) * 1e4


def summarize(root: Path, trades: list[dict]) -> dict:
    vals = [v for v in (taker_slippage_bps(root, {**t, "symbol": t["symbol"]}) for t in trades) if v is not None]
    return {"signals": len(trades), "measured": len(vals), "median_bps": float(np.median(vals)) if vals else None, "p90_bps": float(np.percentile(vals, 90)) if vals else None,
            "note": None if vals else "no recorded book snapshot within 10 s of any signal yet"}


__all__ = ["taker_slippage_bps", "summarize", "CONTRACT_VALUE"]
