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
    from app.services import instrument_kind
    out = [i for i in priority_universe(db, user_id) if not instrument_kind.is_fund(i)]      # ETFs / index / liquid / gilt funds are not stocks: no stock research for them
    if include_master:
        seen = {i.id for i in out}
        try:
            from app.services import lab
            for inst in lab.master_instruments(db, user_id):
                if inst.id not in seen and inst.country == "IN" and inst.asset_class == "equity" and not inst.is_synthetic and not instrument_kind.is_fund(inst):
                    seen.add(inst.id)
                    out.append(inst)
        except Exception as exc:  # noqa: BLE001 - a master-list problem must not stop research of the priority stocks
            logger.warning("research_master_universe_failed error_class=%s", type(exc).__name__)
    return out


FRESH_HOURS = 20   # a run newer than this is current; older ones are redone, so a long gap (app closed for days) is caught up automatically


def researched_on(db: Session, user_id: UUID, instrument_id: UUID, now: datetime, hours: float = FRESH_HOURS) -> bool:
    start = now - timedelta(hours=hours)
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
                            from app.services import report_store
                            report_store.refresh_in_background(user_id)
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


_last_blue_sky_review: str | None = None


def _start_nse_nightly(user_id: UUID) -> None:
    from app.config import settings
    if not (settings.nse_results_enabled or settings.nse_shareholding_enabled):
        return

    def run() -> None:
        try:
            from app.services import nse_data
            with Session(engine) as db:
                out = nse_data.nightly(db, user_id)
                logger.info("nse_nightly stopped=%s stocks=%d", out.get("stopped"), len(out.get("results") or out.get("shareholding") or {}))
        except Exception as exc:  # noqa: BLE001
            logger.warning("nse_nightly_thread_failed error_class=%s", type(exc).__name__)
    threading.Thread(target=run, name="nse-nightly", daemon=True).start()


def _blue_sky_review_once_a_day(now: datetime) -> None:
    """Nexus v2: follow the shadow ladders forward and log the provisional blue-sky review (a WARNING only when the owner must be told). At most once per day; never raises."""
    global _last_blue_sky_review
    today = now.astimezone(IST).date().isoformat()
    if _last_blue_sky_review == today:
        return
    _last_blue_sky_review = today
    try:
        from app.services import multibagger_loader, price_crosscheck, shadow_outcomes
        with Session(engine) as db:
            price_crosscheck.log_nightly(db)          # Dhan vs NSE bhavcopy closes: flags stocks off by more than 1% (excluded from backtests)
            for user_id in db.scalars(select(User.id).where(User.email_verified.is_(True))).all():
                _start_nse_nightly(user_id)               # NSE shareholding + filed results (flags off in code, on in the owner's .env); its own slow, polite thread
                shadow_outcomes.log_review(db, user_id)
                multibagger_loader.log_nightly(db, user_id)      # Phase 6: the engine always runs in shadow mode; the flag only decides whether results are shown or used
    except Exception as exc:  # noqa: BLE001
        logger.warning("blue_sky_review_hook_failed error_class=%s", type(exc).__name__)


def _rebuild_reports(summary: dict[str, int], key: str) -> None:
    """After research ran, rebuild each owner's stored daily report once (in the background): the endpoint serves the stored copy, never a per-request rebuild."""
    if not summary.get(key):
        return
    from app.services import report_store
    with Session(engine) as db:
        for user_id in db.scalars(select(User.id).where(User.email_verified.is_(True))).all():
            report_store.refresh_in_background(user_id)


def run_cycle(now: datetime | None = None) -> dict[str, int]:
    summary = _run_cycle(now)
    _rebuild_reports(summary, "researched")
    return summary


def _run_cycle(now: datetime | None = None) -> dict[str, int]:
    """Research every not-yet-done stock of every verified user, up to the per-cycle cap. Failures are counted, never hidden."""
    now = now or datetime.now(timezone.utc)
    _blue_sky_review_once_a_day(now)
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


RADAR_WINDOW = (dtime(9, 0), dtime(20, 0))     # IST: rechecked through the trading day and after the evening candle, not overnight
_last_radar_pass = 0.0


def radar_pass(now: datetime | None = None) -> dict[str, int]:
    summary = _radar_pass(now)
    _rebuild_reports(summary, "checked")
    return summary


def _radar_pass(now: datetime | None = None) -> dict[str, int]:
    """Re-research stocks on the radar (a buy signal but no entry range yet) far more often than the nightly cycle.

    Technicals use COMPLETED daily candles, so the benefit is catching a fresh candle, a finished history backfill, a reconnected Dhan session, or
    new news/fundamentals quickly. Bounded per pass; the same pipeline, so every number still comes from Python over stored data."""
    from app.services import research_report
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(IST)
    summary = {"checked": 0, "failed": 0}
    if classify_day(local.date(), "equity_in") != TRADING or not (RADAR_WINDOW[0] <= local.time() <= RADAR_WINDOW[1]):
        return summary
    with Session(engine) as db:
        user_ids = list(db.scalars(select(User.id).where(User.email_verified.is_(True))))
    for user_id in user_ids:
        with Session(engine) as db:
            for instrument in user_universe(db, user_id):
                if summary["checked"] + summary["failed"] >= settings.research_radar_max_per_pass:
                    return summary
                latest = research_pipeline.latest_brief(db, user_id, instrument.id)
                if latest is None or not research_report.is_radar(latest[1].sections or {}):
                    continue
                if researched_on(db, user_id, instrument.id, now, settings.research_radar_recheck_hours):
                    continue
                try:
                    research_pipeline.run_instrument_research(db, user_id, instrument)
                    summary["checked"] += 1
                except Exception as exc:  # noqa: BLE001
                    db.rollback()
                    summary["failed"] += 1
                    logger.warning("research_radar_failed symbol=%s error_class=%s", instrument.symbol, type(exc).__name__)
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
        global _last_radar_pass
        if time.monotonic() - _last_radar_pass >= 1800:       # radar stocks are rechecked every 30 minutes (when their last run is old enough)
            _last_radar_pass = time.monotonic()
            try:
                logger.info("research_radar_pass %s", await asyncio.to_thread(radar_pass))
            except Exception as exc:  # noqa: BLE001
                logger.warning("research_radar_pass_failed error_class=%s", type(exc).__name__)
        try:
            await asyncio.to_thread(run_new_stocks_all_users)    # newly added stocks are researched within minutes, not at the next nightly run
        except Exception as exc:  # noqa: BLE001
            logger.warning("research_new_stocks_failed error_class=%s", type(exc).__name__)
        await asyncio.sleep(300)
