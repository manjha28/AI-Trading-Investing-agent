"""Keep enough stored 1-minute history for backtests, in two tiers (owner, 26 Nov 2026): Nifty 50, held stocks and short watchlists get 2 years;
every other Market watch stock gets 6 months. Runs only while NSE is closed, fetches only the 90-day windows that are largely absent, stops before
the database gets too big, and never deletes anything. Read-only market-data calls: no order endpoint is touched."""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, time, timedelta, timezone
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.config import NSE_NON_TRADING_DATES
from app.models import BrokerCredential, BrokerMirror, Instrument, User, UserMarketDataSettings, WatchlistInstrument

logger = logging.getLogger("history_tier_backfill")
IST = ZoneInfo("Asia/Kolkata")
LONG_DAYS, SHORT_DAYS = 730, 183
WINDOW_DAYS = 90
COVERED_SHARE = 0.85                      # a window with at least this share of its weekdays stored is left alone (holidays account for the rest)
MAX_WATCHLIST_FOR_LONG_TIER = 60          # a very long watchlist is a screening list, not favourites
MAX_DATABASE_BYTES = 24 * 1024**3         # the volume is 35 GB: stop well before it fills
CHECK_SECONDS = 600


def quiet_hours(now: datetime) -> bool:
    """Outside the NSE session (with a margin), so the download never competes with live prices."""
    local = now.astimezone(IST)
    if local.weekday() >= 5 or local.date() in NSE_NON_TRADING_DATES:
        return True
    return not time(8, 45) <= local.time() <= time(16, 15)


def windows(now: datetime, days: int) -> list[tuple[datetime, datetime]]:
    """The 90-day windows covering the last ``days`` days up to yesterday, newest first."""
    end = datetime.combine(now.astimezone(IST).date(), time(0, 0), tzinfo=IST)
    start = end - timedelta(days=days)
    out = []
    while end > start:
        begin = max(start, end - timedelta(days=WINDOW_DAYS))
        out.append((begin.astimezone(timezone.utc), end.astimezone(timezone.utc)))
        end = begin
    return out


def covered(stored_days: int, begin: datetime, end: datetime) -> bool:
    first, last = begin.astimezone(IST).date(), end.astimezone(IST).date()
    weekdays = sum(1 for n in range((last - first).days) if (first + timedelta(days=n)).weekday() < 5)
    return weekdays == 0 or stored_days >= weekdays * COVERED_SHARE


def stored_days(db: Session, instrument_id: UUID, begin: datetime, end: datetime) -> int:
    return db.scalar(text("select count(distinct (timestamp at time zone 'Asia/Kolkata')::date) from market_bars "
                          "where instrument_id = :i and interval = '1m' and is_synthetic = false and timestamp >= :a and timestamp < :b"), {"i": instrument_id, "a": begin, "b": end}) or 0


def long_tier_ids(db: Session, user_id: UUID, instruments: list[Instrument]) -> set[UUID]:
    from app.strategies.universes import NIFTY_50

    held = {s.strip().upper().removesuffix("-EQ") for s in db.scalars(select(BrokerMirror.symbol).where(
        BrokerMirror.user_id == user_id, BrokerMirror.broker == "dhan", BrokerMirror.kind == "holding"))}
    watch = set(db.scalars(select(WatchlistInstrument.instrument_id).where(WatchlistInstrument.user_id == user_id)))
    favourites = watch if len(watch) <= MAX_WATCHLIST_FOR_LONG_TIER else set()
    return {i.id for i in instruments if i.symbol in NIFTY_50 or i.symbol.upper() in held or i.id in favourites}


def run_once(clock=lambda: datetime.now(timezone.utc)) -> dict[str, int] | None:
    """One pass. Stops early (to be resumed later) when the market is about to open or the database is large. None: no Dhan user."""
    from app.api import market_data as md
    from app.db import engine
    from app.services import history_backfill, lab

    with Session(engine) as db:
        user = db.scalar(select(User).join(UserMarketDataSettings, UserMarketDataSettings.user_id == User.id).join(BrokerCredential, BrokerCredential.user_id == User.id)
                         .where(User.email_verified.is_(True), UserMarketDataSettings.selected_provider == "dhan", BrokerCredential.broker == "dhan")
                         .order_by(User.created_at).limit(1))
        if user is None:
            return None
        provider = md.market_data_provider_for_user(db, user.id)
        instruments = lab.master_instruments(db, user.id)
        long_ids = long_tier_ids(db, user.id, instruments)
        instruments.sort(key=lambda i: (i.id not in long_ids, i.symbol))        # the long tier first
        stats = {"instruments": len(instruments), "long_tier": len(long_ids), "windows": 0, "failed": 0, "stopped_early": 0}
        for instrument in instruments:
            symbol, instrument_id = instrument.symbol, instrument.id
            for begin, end in windows(clock(), LONG_DAYS if instrument_id in long_ids else SHORT_DAYS):
                if covered(stored_days(db, instrument_id, begin, end), begin, end):
                    continue
                if not quiet_hours(clock()) or db.scalar(text("select pg_database_size(current_database())")) > MAX_DATABASE_BYTES:
                    stats["stopped_early"] = 1
                    return stats
                reason = history_backfill._sync(md, db, user, provider, db.get(Instrument, instrument_id), begin, end, "1m",
                                                min(md.MAX_DHAN_1M_BACKFILL_BARS, md.MAX_DHAN_1M_CHUNK_BARS))
                stats["windows"] += 1
                if reason:
                    stats["failed"] += 1
                    logger.warning("history_tier_window_failed symbol=%s reason=%s", symbol, reason[:120])
                if stats["windows"] % 100 == 0:
                    logger.warning("history_tier_progress windows=%s failed=%s at=%s", stats["windows"], stats["failed"], symbol)
        return stats


async def history_tier_backfill_loop() -> None:
    done_for: date | None = None
    await asyncio.sleep(120)
    while True:
        try:
            now = datetime.now(timezone.utc)
            today = now.astimezone(IST).date()
            if quiet_hours(now) and done_for != today:
                stats = await asyncio.to_thread(run_once)
                if stats is not None:
                    logger.warning("history_tier_pass %s", " ".join(f"{k}={v}" for k, v in stats.items()))
                    if not stats["stopped_early"]:
                        done_for = today
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - retried at the next check
            logger.warning("history_tier_error error_class=%s", type(error).__name__)
        await asyncio.sleep(CHECK_SECONDS)
