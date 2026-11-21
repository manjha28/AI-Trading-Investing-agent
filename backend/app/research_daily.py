"""Daily backend research for each user's universe: watchlists, held stocks, then the whole Market watch master list.

Runs after the NSE close on trading days (and once at startup to catch up), one instrument at a time, using the same pipeline as the Research page, so every figure still
comes from Python over stored data. A stock already researched today (IST) is skipped. Nothing here places an order or implies one.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from datetime import datetime, time as dtime, timedelta, timezone
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import engine
from app.models import (Instrument, InstrumentResearchRun, Portfolio, PortfolioTransaction, User, Watchlist, WatchlistInstrument)
from app.services import research_pipeline
from app.services.trading_calendar import TRADING, classify_day

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")
RUN_AFTER = dtime(16, 30)   # after the NSE close, once the day's final candle is published


def due_today(now: datetime) -> bool:
    local = now.astimezone(IST)
    return local.time() >= RUN_AFTER and classify_day(local.date(), "equity_in") == TRADING


def priority_universe(db: Session, user_id: UUID) -> list[Instrument]:
    """Watchlists plus every stock with a recorded portfolio transaction (researched first, and immediately when newly added)."""
    watched = select(WatchlistInstrument.instrument_id).join(Watchlist, Watchlist.id == WatchlistInstrument.watchlist_id).where(Watchlist.user_id == user_id)
    held = select(PortfolioTransaction.instrument_id).join(Portfolio, Portfolio.id == PortfolioTransaction.portfolio_id).where(
        Portfolio.user_id == user_id, PortfolioTransaction.instrument_id.is_not(None))
    return list(db.scalars(select(Instrument).where(
        Instrument.id.in_(watched) | Instrument.id.in_(held), Instrument.country == "IN", Instrument.asset_class == "equity",
        Instrument.is_synthetic.is_(False)).order_by(Instrument.symbol)))


def user_universe(db: Session, user_id: UUID, *, include_master: bool = True) -> list[Instrument]:
    """Priority stocks first, then the whole Market watch master list (the owner's scan universe). De-duplicated, NSE equity only."""
    out = priority_universe(db, user_id)
    if include_master:
        seen = {i.id for i in out}
        try:
            from app.services import lab
            for inst in lab.master_instruments(db, user_id):
                if inst.id not in seen and inst.country == "IN" and inst.asset_class == "equity" and not inst.is_synthetic:
                    seen.add(inst.id)
                    out.append(inst)
        except Exception as exc:  # noqa: BLE001 - a master-list problem must not stop research of the priority stocks
            logger.warning("research_master_universe_failed error_class=%s", type(exc).__name__)
    return out


FRESH_HOURS = 20   # a run newer than this is current; older ones are redone, so a long gap (app closed for days) is caught up automatically


def researched_on(db: Session, user_id: UUID, instrument_id: UUID, now: datetime) -> bool:
    start = now - timedelta(hours=FRESH_HOURS)
    return db.scalar(select(InstrumentResearchRun.id).where(
        InstrumentResearchRun.user_id == user_id, InstrumentResearchRun.instrument_id == instrument_id,
        InstrumentResearchRun.status.in_(("complete", "partial", "running")), InstrumentResearchRun.started_at >= start).limit(1)) is not None


_IN_FLIGHT: set[tuple[UUID, UUID]] = set()
_IN_FLIGHT_LOCK = threading.Lock()


def pending_new(db: Session, user_id: UUID) -> list[Instrument]:
    """Stocks in the user's universe with no research run at all yet (newly added)."""
    return [i for i in user_universe(db, user_id, include_master=False) if db.scalar(select(InstrumentResearchRun.id).where(
        InstrumentResearchRun.user_id == user_id, InstrumentResearchRun.instrument_id == i.id).limit(1)) is None]


def kick_missing(user_id: UUID) -> list[str]:
    """Start research in a background thread for newly added stocks; returns their symbols (so the page can show 'researching')."""
    with Session(engine) as db:
        todo = [(i.id, i.symbol) for i in pending_new(db, user_id)]
    started = [sym for iid, sym in todo if (user_id, iid) in _IN_FLIGHT]
    fresh = [(iid, sym) for iid, sym in todo if (user_id, iid) not in _IN_FLIGHT]
    if fresh:
        with _IN_FLIGHT_LOCK:
            for iid, _ in fresh:
                _IN_FLIGHT.add((user_id, iid))

        def work() -> None:
            for iid, sym in fresh:
                try:
                    with Session(engine) as db:
                        instrument = db.get(Instrument, iid)
                        if instrument is not None:
                            research_pipeline.run_instrument_research(db, user_id, instrument)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("research_new_stock_failed symbol=%s error_class=%s", sym, type(exc).__name__)
                finally:
                    with _IN_FLIGHT_LOCK:
                        _IN_FLIGHT.discard((user_id, iid))
        threading.Thread(target=work, name="research-new-stocks", daemon=True).start()
    return started + [sym for _, sym in fresh]


def run_new_stocks_all_users() -> int:
    with Session(engine) as db:
        user_ids = list(db.scalars(select(User.id).where(User.email_verified.is_(True))))
    return sum(len(kick_missing(u)) for u in user_ids)


def run_cycle(now: datetime | None = None) -> dict[str, int]:
    """Research every not-yet-done stock of every verified user, up to the per-cycle cap. Failures are counted, never hidden."""
    now = now or datetime.now(timezone.utc)
    summary = {"researched": 0, "skipped": 0, "failed": 0}
    with Session(engine) as db:
        user_ids = list(db.scalars(select(User.id).where(User.email_verified.is_(True))))
    for user_id in user_ids:
        with Session(engine) as db:
            try:   # the benchmark (NIFTY 50 or a tracking ETF) needs history too, or relative strength stays "not scored"
                for sym in research_pipeline.BENCHMARK_SYMBOLS:
                    bench = db.scalar(select(Instrument).where(func.upper(Instrument.symbol) == sym, Instrument.is_synthetic.is_(False)).limit(1))
                    if bench is not None:
                        research_pipeline.ensure_daily_history(db, bench, user_id)
                        break
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                logger.warning("research_benchmark_history_failed error_class=%s", type(exc).__name__)
            for instrument in user_universe(db, user_id):
                if summary["researched"] + summary["failed"] >= settings.research_daily_max_per_cycle:
                    return summary
                if researched_on(db, user_id, instrument.id, now):
                    summary["skipped"] += 1
                    continue
                try:
                    research_pipeline.run_instrument_research(db, user_id, instrument)
                    summary["researched"] += 1
                except Exception as exc:  # noqa: BLE001 - one stock must not stop the batch
                    db.rollback()
                    summary["failed"] += 1
                    logger.warning("research_daily_failed symbol=%s error_class=%s", instrument.symbol, type(exc).__name__)
                time.sleep(settings.research_daily_pause_seconds)
    return summary


async def research_daily_loop() -> None:
    await asyncio.sleep(120)
    first = True    # on startup always catch up anything older than FRESH_HOURS, even outside the nightly window
    while True:
        try:
            if first or due_today(datetime.now(timezone.utc)):
                first = False
                summary = await asyncio.to_thread(run_cycle)
                logger.info("research_daily_cycle %s", summary)
        except Exception as exc:  # noqa: BLE001 - the loop must survive any single failure
            logger.warning("research_daily_cycle_failed error_class=%s", type(exc).__name__)
        try:
            await asyncio.to_thread(run_new_stocks_all_users)    # newly added stocks are researched within minutes, not at the next nightly run
        except Exception as exc:  # noqa: BLE001
            logger.warning("research_new_stocks_failed error_class=%s", type(exc).__name__)
        await asyncio.sleep(300)
