"""Keep the stored DAILY closes of the whole Market watch master list current, after every NSE session.

Why this exists: the only scheduled close capture covered watchlist and portfolio instruments, and "Load missing closes" was a manual button, so on
23 Nov 360 of 367 master-list instruments still had 20 Nov as their last close. This job runs when the market is closed, finds every instrument whose newest
real Dhan daily bar is older than the expected session, and pulls the recent daily candles for it, stalest first, within a time budget (Dhan is paced at one call a
second). A failure is recorded, never hidden, and the next pass continues. Real Dhan data only.
"""
from __future__ import annotations

import asyncio
import logging
import time as _time
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import engine
from app.models import BrokerCredential, Instrument, MarketBar, User, UserMarketDataSettings

logger = logging.getLogger("daily_close_refresh")
LOOKBACK_DAYS = 12
IST = ZoneInfo("Asia/Kolkata")


PUBLISHED_AFTER = time(16, 15)        # IST: a daily bar fetched before this on its own session day came from a quote snapshot, not the published candle


def needs_refresh(last_bar_date: date | None, expected: date, retrieved_at: datetime | None = None) -> bool:
    """True when the newest daily bar is missing, older than the session that should already be closed, or only a provisional quote snapshot of that session."""
    if last_bar_date is None or last_bar_date < expected:
        return True
    if retrieved_at is None:
        return False
    local = (retrieved_at if retrieved_at.tzinfo else retrieved_at.replace(tzinfo=timezone.utc)).astimezone(IST)
    return local.date() == expected and local.time() < PUBLISHED_AFTER


def stale_instruments(db: Session, instruments: list[Instrument], now: datetime) -> list[tuple[Instrument, date | None]]:
    from app.api import market_data as md

    if not instruments:
        return []
    rows = db.execute(select(MarketBar.instrument_id, MarketBar.timestamp, MarketBar.retrieved_at).where(
        MarketBar.instrument_id.in_([i.id for i in instruments]), MarketBar.interval == "1d", MarketBar.source == "dhan", MarketBar.is_synthetic.is_(False)).order_by(MarketBar.timestamp.desc())).all()
    newest: dict = {}
    for iid, ts, got in rows:
        newest.setdefault(iid, (ts.date(), got))                                   # newest bar per instrument
    out = [(i, newest[i.id][0] if i.id in newest else None) for i in instruments
           if needs_refresh(newest[i.id][0] if i.id in newest else None, md._expected_market_session_date(i, now), newest[i.id][1] if i.id in newest else None)]
    return sorted(out, key=lambda pair: (pair[1] is not None, pair[1] or date.min))             # never fetched first, then the stalest


def run_pass(now: datetime | None = None, *, budget_seconds: float | None = None) -> dict[str, int]:
    from app.api import market_data as md
    from app.services import forward_test as fts
    from app.services import lab

    now = now or datetime.now(timezone.utc)
    if fts.session_open(now):
        return {"users": 0, "stale": 0, "refreshed": 0, "errors": 0}        # during the session the daily candle is still forming: never store it as a close
    budget = budget_seconds if budget_seconds is not None else float(settings.daily_close_refresh_budget_seconds)
    started = _time.monotonic()
    users = stale = refreshed = errors = 0
    with Session(engine) as db:
        wanted = list(db.scalars(select(User).join(UserMarketDataSettings, UserMarketDataSettings.user_id == User.id).join(BrokerCredential, BrokerCredential.user_id == User.id).where(
            User.email_verified.is_(True), UserMarketDataSettings.selected_provider == "dhan", BrokerCredential.broker == "dhan")))
        for user in wanted:
            instruments = lab.master_instruments(db, user.id)
            todo = stale_instruments(db, instruments, now)
            if not todo:
                continue
            users += 1
            stale += len(todo)
            try:
                provider = md.market_data_provider_for_user(db, user.id)
            except Exception:  # noqa: BLE001 - an expired Dhan token is reported by the connection banner, not retried here
                errors += len(todo)
                continue
            for inst, _last in todo:
                if _time.monotonic() - started > budget:
                    break
                try:
                    md.sync_history(instrument_id=inst.id, start=now - timedelta(days=LOOKBACK_DAYS), end=now, interval="1d", limit=30, provider=provider, _user=user, db=db, background_tasks=None)
                    refreshed += 1
                except Exception:  # noqa: BLE001
                    db.rollback()
                    errors += 1
    return {"users": users, "stale": stale, "refreshed": refreshed, "errors": errors}


async def daily_close_refresh_loop() -> None:
    while True:
        try:
            if settings.daily_close_refresh_enabled:
                result = await asyncio.to_thread(run_pass)
                if result["stale"]:
                    logger.info("daily_close_refresh %s", result)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("daily_close_refresh_failed")
        await asyncio.sleep(300)
