"""Bulk historical backfill of REAL Dhan bars for the owner's Market watch master list.

Fills daily bars for the past N years (default 6) and 1-minute bars for the past N days (default 365) so backtests and scans have enough
history to find every instrument eligible. Idempotent and resumable: ``sync_history`` only requests ranges not already stored, so a re-run
continues where the last one stopped. Nothing is padded or synthesised; what Dhan does not return stays missing and is reported.

Threads: every worker uses its own DB session. Outbound Dhan requests are still paced globally (1/s per host, ``_wait_for_provider``), so
threads overlap DB writes and parsing, not provider calls. A 429 from any worker pauses all workers.

Run from ``backend/``:  python -m app.services.history_backfill [--user-id UUID] [--workers 4] [--limit N] [--dry-run]
"""
from __future__ import annotations

import argparse
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import BrokerCredential, Instrument, User, UserMarketDataSettings
from app.services.intraday_backfill import CHUNK_DAYS, RATE_LIMIT_PAUSES_SECONDS, _attempts_of, _rate_limited

logger = logging.getLogger("history_backfill")

DAILY_LIMIT_BARS = 3000          # ~12 years of sessions; the request window, not this, bounds the fetch
_pause_until = 0.0               # monotonic time before which no worker may call the provider (set on rate limit)
_pause_lock = threading.Lock()


def _wait_if_paused() -> None:
    while True:
        with _pause_lock:
            remaining = _pause_until - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 5.0))


def _pause_all(seconds: float) -> None:
    global _pause_until
    with _pause_lock:
        _pause_until = max(_pause_until, time.monotonic() + seconds)


def _sync(md: Any, db: Session, user: User, provider: Any, instrument: Instrument, start: datetime, end: datetime, interval: str, limit: int) -> str | None:
    """One window with rate-limit retries. Returns None on success (including "nothing new"), else a failure reason."""
    for attempt in range(len(RATE_LIMIT_PAUSES_SECONDS) + 1):
        _wait_if_paused()
        attempts: list[dict[str, Any]] = []
        try:
            result = md.sync_history(instrument_id=instrument.id, start=start, end=end, interval=interval, limit=limit, provider=provider,
                                     _user=user, db=db, background_tasks=None)
            attempts = _attempts_of(result)
            unavailable = result.sync_status == "unavailable"
        except HTTPException as error:
            db.rollback()
            detail = error.detail if isinstance(error.detail, dict) else {}
            attempts = list(detail.get("provider_attempts", []))
            unavailable = True
        except Exception as error:  # noqa: BLE001 - recorded per instrument, never raised into the pool
            db.rollback()
            return f"{type(error).__name__}: {str(error)[:120]}"
        if not unavailable:
            return None
        if _rate_limited(attempts) and attempt < len(RATE_LIMIT_PAUSES_SECONDS):
            _pause_all(RATE_LIMIT_PAUSES_SECONDS[attempt])
            continue
        first = next((a for a in attempts if a.get("provider") == "dhan"), attempts[0] if attempts else {})
        return "provider_rate_limited_after_retries" if _rate_limited(attempts) else (
            "provider_history_unavailable: " + str(first.get("provider_error_code") or first.get("reason") or "")[:120])
    return "provider_rate_limited_after_retries"


def _fill_one(user_id: UUID, instrument_id: UUID, now: datetime, daily_years: int, minute_days: int) -> dict[str, Any]:
    from app.api import market_data as md
    from app.db import Session as SessionFactory, engine

    out: dict[str, Any] = {"instrument_id": str(instrument_id), "symbol": None, "daily": None, "minute": None}
    with SessionFactory(engine) as db:
        user, instrument = db.get(User, user_id), db.get(Instrument, instrument_id)
        if user is None or instrument is None:
            return {**out, "daily": "instrument_or_owner_unavailable", "minute": "instrument_or_owner_unavailable"}
        out["symbol"] = instrument.symbol
        provider = md.market_data_provider_for_user(db, user_id)
        if daily_years > 0:
            out["daily"] = _sync(md, db, user, provider, instrument, now - timedelta(days=int(daily_years * 365.25)), now, "1d", DAILY_LIMIT_BARS)
        if minute_days > 0:
            failure: str | None = None
            chunk_end, start = now, now - timedelta(days=minute_days)
            while chunk_end > start and failure is None:
                chunk_start = max(start, chunk_end - timedelta(days=CHUNK_DAYS))
                failure = _sync(md, db, user, provider, instrument, chunk_start, chunk_end, "1m", min(md.MAX_DHAN_1M_BACKFILL_BARS, md.MAX_DHAN_1M_CHUNK_BARS))
                chunk_end = chunk_start
            out["minute"] = failure
    return out


def _default_user(db: Session) -> User:
    users = list(db.scalars(select(User).join(UserMarketDataSettings, UserMarketDataSettings.user_id == User.id)
                            .join(BrokerCredential, BrokerCredential.user_id == User.id)
                            .where(User.email_verified.is_(True), UserMarketDataSettings.selected_provider == "dhan", BrokerCredential.broker == "dhan")))
    if len(users) != 1:
        raise SystemExit(f"Expected exactly one verified user with Dhan as data source, found {len(users)}; pass --user-id.")
    return users[0]


def run(user_id: UUID | None = None, *, workers: int = 4, daily_years: int = 6, minute_days: int = 365, limit: int | None = None,
        dry_run: bool = False) -> dict[str, Any]:
    from app.db import Session as SessionFactory, engine
    from app.services import lab

    now = datetime.now(timezone.utc)
    with SessionFactory(engine) as db:
        user = db.get(User, user_id) if user_id else _default_user(db)
        if user is None:
            raise SystemExit("User not found.")
        instruments = lab.master_instruments(db, user.id)
        uid = user.id
        ids = [i.id for i in instruments][:limit]
    summary: dict[str, Any] = {"instruments": len(ids), "daily_failed": [], "minute_failed": [], "done": 0}
    if dry_run:
        return summary
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(_fill_one, uid, i, now, daily_years, minute_days) for i in ids]
        for future in as_completed(futures):
            r = future.result()
            summary["done"] += 1
            if r["daily"]:
                summary["daily_failed"].append((r["symbol"], r["daily"]))
            if r["minute"]:
                summary["minute_failed"].append((r["symbol"], r["minute"]))
            print(f"[{summary['done']}/{len(ids)}] {r['symbol']} daily={'ok' if not r['daily'] else r['daily']} "
                  f"1m={'ok' if not r['minute'] else r['minute']} elapsed={time.monotonic() - started:.0f}s", flush=True)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", type=UUID)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--daily-years", type=int, default=6)
    parser.add_argument("--minute-days", type=int, default=365)
    parser.add_argument("--limit", type=int, help="only the first N master-list instruments (for a trial run)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)
    result = run(args.user_id, workers=args.workers, daily_years=args.daily_years, minute_days=args.minute_days, limit=args.limit, dry_run=args.dry_run)
    print({k: (v if not isinstance(v, list) else f"{len(v)} {v[:10]}") for k, v in result.items()})


if __name__ == "__main__":
    main()
