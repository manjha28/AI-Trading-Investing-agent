"""What the MCX watch shows while the market is closed: the last session's figures per contract.

Order of preference per contract: (1) the snapshot stored from live ticks, (2) Dhan's last quote plus the last completed
daily bar (open, high, low, close, volume, open interest), (3) nothing (shown as unknown, never zero). Every field is a string
or None; the figures come from Dhan, never from a model.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.models.mcx_snapshot import McxLastSnapshot

logger = logging.getLogger(__name__)
THROTTLE_SECONDS = 30
_last_saved: dict[str, float] = {}


def save_snapshot(db: Session, security_id: str, symbol: str, payload: dict[str, Any], session_date: date | None, *, force: bool = False, now_monotonic: float | None = None) -> bool:
    """Store the latest tick figures; at most one write per contract per THROTTLE_SECONDS unless forced (session end)."""
    import time

    stamp = time.monotonic() if now_monotonic is None else now_monotonic
    if not force and stamp - _last_saved.get(security_id, -1e9) < THROTTLE_SECONDS:
        return False
    row = db.get(McxLastSnapshot, security_id)
    now = datetime.now(timezone.utc)
    clean = {k: (None if v is None else str(v)) for k, v in payload.items()}
    if row is None:
        db.add(McxLastSnapshot(security_id=security_id, symbol=symbol, payload=clean, session_date=session_date, captured_at=now))
    else:
        row.symbol, row.payload, row.session_date, row.captured_at = symbol, clean, session_date, now
    db.commit()
    _last_saved[security_id] = stamp
    return True


def load_snapshots(db: Session, security_ids: list[str]) -> dict[str, McxLastSnapshot]:
    rows = [db.get(McxLastSnapshot, sid) for sid in security_ids]
    return {r.security_id: r for r in rows if r is not None}


def closed_tick(key: str, symbol: str, p: dict[str, Any], *, source: str) -> dict[str, Any]:
    """The tick message for a closed market, with the change recomputed in Decimal from last and previous close."""
    last = Decimal(p["ltp"]) if p.get("ltp") else None
    prev = Decimal(p["previous_close"]) if p.get("previous_close") else None
    change = last - prev if last is not None and prev else None
    return {
        "type": "tick", "key": key, "symbol": symbol, "ltp": str(last) if last is not None else None,
        "previous_close": str(prev) if prev else None,
        "change": str(change) if change is not None else None,
        "change_pct": str((change / prev * Decimal("100")).quantize(Decimal("0.01"))) if change is not None and prev else None,
        "open": p.get("open"), "high": p.get("high"), "low": p.get("low"), "volume": p.get("volume"), "oi": p.get("oi"),
        "tick_time": p.get("tick_time"), "session_date": p.get("session_date"), "last_close": True, "source": source,
        "received_at": datetime.now(timezone.utc).isoformat(),
    }
