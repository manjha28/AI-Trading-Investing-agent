"""Forward testing on LIVE market data: keep 1-minute bars fresh, replay the strategy causally from the start date at a fixed quantity,
and snapshot the result. The replay is the same deterministic engine as a backtest, so it can never see a candle before it closes.

Paper results are hypothetical (what the strategy would have done) and are stored separately from real P&L. Live order placement,
when allowed, happens in ``dhan_execution`` and only on a fresh signal from the latest completed candle.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

import pandas as pd
import time as _time

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.dhan_execution import policy
from app.dhan_execution.client import DhanOrderClient
from app.dhan_execution.intent import DhanOrderIntent
from app.dhan_execution.service import ClientFactory, attempt_order
from app.models import Instrument, InstrumentProviderIdentifier, MarketBar, User
from app.models.forward_live import ForwardTest
from app.services import intraday_runner as runner
from app.services import lab
from app.services.intraday_backtest import BacktestConfig
from app.services.intraday_strategy import Sizing, StrategyDefinition
from app.services.timeframes import MINUTE, TIMEFRAME_MINUTES

logger = logging.getLogger("forward_test")
IST = ZoneInfo("Asia/Kolkata")
WARMUP_CALENDAR_DAYS = 12
STALE_AFTER_MINUTES = 6
TICK = Decimal("0.05")
LABEL = "Paper forward test: hypothetical trades at a fixed quantity on live data, after Indian charges and slippage. Not real P&L, and no order is placed by this view."


def aware(value: datetime | None) -> datetime | None:
    """Stored timestamps are UTC; some databases hand them back without a timezone."""
    return None if value is None else value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def session_open(now: datetime) -> bool:
    local = now.astimezone(IST)
    return local.weekday() < 5 and time(9, 15) <= local.time() < time(15, 30)


def next_trading_day(today: date) -> date:
    """The next weekday after ``today`` (exchange holidays are not known here: a holiday simply produces no bars and so no trades)."""
    day = today + timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


def start_of(ft_start: date) -> datetime:
    return datetime.combine(ft_start, time(0, 0), tzinfo=IST).astimezone(timezone.utc)


def _round_tick(value: Decimal) -> Decimal:
    return (value / TICK).quantize(Decimal(1), rounding=ROUND_HALF_UP) * TICK


# ───────────────────────── data ─────────────────────────
FRESH_WITHIN_SECONDS = 90


def last_bar_times(db: Session, ids: list[UUID]) -> dict[UUID, datetime | None]:
    rows = db.execute(select(MarketBar.instrument_id, func.max(MarketBar.timestamp)).where(
        MarketBar.instrument_id.in_(ids), MarketBar.interval == "1m", MarketBar.is_synthetic.is_(False)).group_by(MarketBar.instrument_id)).all()
    found = {r[0]: aware(r[1]) for r in rows}
    return {i: found.get(i) for i in ids}


def refresh_bars(db: Session, user: User, instruments: list[Instrument], now: datetime, *, max_seconds: float | None = None) -> dict[UUID, str | None]:
    """Pull the newest real 1-minute bars from Dhan, STALEST symbols first, within an optional time budget.

    Dhan data calls are paced at one per second, so a big list cannot be refreshed in one pass: each pass spends its budget on the symbols that
    need it most and the next pass continues from there. A failure is recorded per instrument, never hidden. Instruments left for a later
    pass are simply not in the result.
    """
    from app.api import market_data as md

    errors: dict[UUID, str | None] = {}
    try:
        provider = md.market_data_provider_for_user(db, user.id)
    except Exception as error:  # noqa: BLE001
        return {i.id: f"market-data provider unavailable: {type(error).__name__}" for i in instruments}
    lasts = last_bar_times(db, [i.id for i in instruments])
    live = session_open(now)
    order = sorted(instruments, key=lambda i: (lasts[i.id] is not None, lasts[i.id] or datetime.min.replace(tzinfo=timezone.utc)))
    started = _time.monotonic()
    for inst in order:
        if max_seconds is not None and _time.monotonic() - started > max_seconds:
            break
        last_time = lasts[inst.id]
        if live and last_time is not None and (now - last_time).total_seconds() < FRESH_WITHIN_SECONDS:
            continue                                                  # already current: spend the budget elsewhere
        try:
            start = (last_time - timedelta(minutes=10)) if last_time is not None and now - last_time < timedelta(days=2) else now - timedelta(days=WARMUP_CALENDAR_DAYS + 4)
            result = md.sync_history(instrument_id=inst.id, start=start, end=now, interval="1m", limit=min(md.MAX_DHAN_1M_BACKFILL_BARS, md.MAX_DHAN_1M_CHUNK_BARS),
                                     provider=provider, _user=user, db=db, background_tasks=None)
            errors[inst.id] = "provider returned no data" if getattr(result, "sync_status", "") == "unavailable" else None
        except Exception as error:  # noqa: BLE001
            db.rollback()
            errors[inst.id] = f"{type(error).__name__}: {str(getattr(error, 'detail', error))[:120]}"
    return errors


def uses_master(ft: ForwardTest) -> bool:
    return lab.MASTER_TOKEN in (ft.instrument_ids or [])


def instruments_of(db: Session, ft: ForwardTest) -> list[Instrument]:
    """The test's instruments; ``@master`` follows the user's Market watch list as it changes."""
    explicit = [i for i in (db.get(Instrument, UUID(x)) for x in ft.instrument_ids if x != lab.MASTER_TOKEN) if i is not None]
    if not uses_master(ft):
        return explicit
    seen = {i.id for i in explicit}
    return explicit + [i for i in lab.master_instruments(db, ft.user_id) if i.id not in seen]


# ───────────────────────── paper replay ─────────────────────────
def _defn(ft: ForwardTest) -> StrategyDefinition:
    d = StrategyDefinition.model_validate(ft.definition)
    return d.model_copy(update={"sizing": Sizing(method="fixed_quantity", value=float(ft.quantity))})


def _summary(trades: list[dict[str, Any]]) -> dict[str, Any]:
    wins = [t for t in trades if Decimal(t["net_pnl"]) > 0]
    total = lambda key: sum((Decimal(t[key]) for t in trades), Decimal(0))  # noqa: E731
    by_symbol: dict[str, dict[str, Any]] = {}
    for t in trades:
        row = by_symbol.setdefault(t["symbol"], {"trades": 0, "net_pnl": Decimal(0), "wins": 0})
        row["trades"] += 1
        row["net_pnl"] += Decimal(t["net_pnl"])
        row["wins"] += Decimal(t["net_pnl"]) > 0
    return {"closed_trades": len(trades), "wins": len(wins), "losses": len(trades) - len(wins),
            "win_rate": round(len(wins) / len(trades), 4) if trades else None,
            "gross_pnl": str(total("gross_pnl")), "charges": str(total("charges")), "slippage": str(total("slippage_cost")), "net_pnl": str(total("net_pnl")),
            "by_symbol": {s: {"trades": v["trades"], "wins": v["wins"], "net_pnl": str(v["net_pnl"])} for s, v in sorted(by_symbol.items())}}


def replay(db: Session, ft: ForwardTest, instruments: list[Instrument], now: datetime, refresh_errors: dict[UUID, str | None]) -> tuple[dict[str, Any], list[Any]]:
    defn = _defn(ft)
    start = start_of(ft.start_date)
    # a test registered well ahead of its start date must still have a valid (if empty) data window
    cfg = BacktestConfig(start=min(start - timedelta(days=WARMUP_CALENDAR_DAYS), now - timedelta(days=1)), end=now, initial_capital=Decimal("1000000"), max_concurrent=max(1, len(instruments)),
                         slippage_bps=ft.slippage_bps, cost_profile=ft.cost_profile)
    runs, refused = runner.prepare_runs(db, defn, instruments, cfg, now)
    universe: list[dict[str, Any]] = []
    refused_ids = {r["instrument_id"]: r["reason"] for r in refused}
    run_by_id = {r.instrument_id: r for r in runs}
    for inst in instruments:
        entry: dict[str, Any] = {"instrument_id": str(inst.id), "symbol": inst.symbol, "status": "ok", "reason": refresh_errors.get(inst.id)}
        run = run_by_id.get(str(inst.id))
        if str(inst.id) in refused_ids:
            entry.update(status="refused", reason=refused_ids[str(inst.id)])
        elif run is not None and len(run.frame):
            closed_at = run.frame["time"].iloc[-1] + MINUTE
            age = (pd.Timestamp(now) - closed_at).total_seconds() / 60
            entry.update(last_bar_close=closed_at.isoformat(), age_minutes=round(age, 1), bars=int(len(run.frame)))
            if session_open(now) and age > STALE_AFTER_MINUTES:
                entry.update(status="stale", reason=entry["reason"] or f"newest bar is {age:.0f} minutes old")
        else:
            entry.update(status="no_data", reason=entry["reason"] or "no stored 1-minute bars")
        universe.append(entry)
    snapshot: dict[str, Any] = {"as_of": now.isoformat(), "start_date": ft.start_date.isoformat(), "quantity": ft.quantity, "session_open": session_open(now), "label": LABEL,
                                "paper_only": True, "universe": universe, "execute_live": ft.execute_live}
    if now < start:
        snapshot.update(waiting=f"Starts {ft.start_date.isoformat()} at 09:15 IST", summary=_summary([]), trades=[], open_positions=[])
        return snapshot, runs
    if not runs:
        snapshot.update(summary=_summary([]), trades=[], open_positions=[], warning="No instrument has usable 1-minute data right now.")
        return snapshot, runs
    result = runner.make_backtester(None)(defn, runs, cfg)
    forward = [t for t in result["trades"] if pd.Timestamp(t["entry_time"]) >= pd.Timestamp(start)]
    keep = ("symbol", "side", "qty", "signal_time", "entry_time", "exit_time", "entry_fill", "exit_fill", "exit_reason", "stop_initial", "target", "gross_pnl",
            "slippage_cost", "charges", "net_pnl", "r_multiple")
    closed = [t for t in forward if t["exit_reason"] != "end_of_data"]
    open_ = [t for t in forward if t["exit_reason"] == "end_of_data"]
    snapshot.update(summary=_summary(closed), trades=[{k: t.get(k) for k in keep} for t in closed[-300:]],
                    open_positions=[{k: t.get(k) for k in ("symbol", "side", "qty", "entry_time", "entry_fill", "stop_initial", "target")} | {"mark": t.get("exit_fill")} for t in open_],
                    skipped_signals=result.get("skipped_signals", {}), assumptions={"cost_profile": (result.get("assumptions") or {}).get("cost_profile", {}).get("label"), "slippage_bps": str(ft.slippage_bps)})
    return snapshot, runs


# ───────────────────────── live signal detection ─────────────────────────
def _security_id(db: Session, inst: Instrument) -> str | None:
    ident = db.scalar(select(InstrumentProviderIdentifier).where(InstrumentProviderIdentifier.instrument_id == inst.id, InstrumentProviderIdentifier.provider == "dhan",
                                                                 InstrumentProviderIdentifier.is_verified.is_(True)))
    head = (ident.provider_instrument_id if ident else "").split("|")[0]
    return head if head.isdigit() else None


def detect_intents(db: Session, ft: ForwardTest, inst: Instrument, run: Any, now: datetime) -> list[DhanOrderIntent]:
    """Entry intents from the LATEST completed decision candle only, and only while that candle is fresh. Levels are computed from data, never typed."""
    defn = _defn(ft)
    if defn.stop.mode == "none" or defn.target.mode == "none":
        return []                                                     # live orders always carry a stop and a target
    res = runner.evaluate_any(defn, None, run.frame, run.profile)
    if len(res.decision_idx) == 0:
        return []
    last = int(res.decision_idx[-1])
    signal_time = (run.frame["time"].iloc[last] + MINUTE).to_pydatetime()
    if now - signal_time > timedelta(minutes=TIMEFRAME_MINUTES[defn.decision_timeframe] + 2):
        return []                                                     # a late signal is not acted on
    security_id = _security_id(db, inst)
    if security_id is None:
        return []
    ref = Decimal(repr(float(run.frame["close"].iloc[last])))
    out: list[DhanOrderIntent] = []
    for rule, side, allowed in (("long_entry", "buy", defn.constraints.allow_long), ("short_entry", "sell", defn.constraints.allow_short)):
        if not allowed or not res.signals[rule][last]:
            continue
        sd: Decimal | None = None
        if defn.stop.mode == "level":
            level = res.stop_levels.get("long" if side == "buy" else "short")
            lv = None if level is None else level[last]
            sd = None if lv is None or lv != lv else abs(ref - Decimal(repr(float(lv))))
        elif defn.stop.mode == "percent":
            sd = ref * Decimal(str(defn.stop.value)) / 100
        elif defn.stop.mode == "atr":
            atr = res.atr.get(defn.stop.atr_length)
            sd = None if atr is None or atr[last] != atr[last] else Decimal(repr(float(atr[last]))) * Decimal(str(defn.stop.value))
        if not sd or sd <= 0:
            continue
        td = sd * Decimal(str(defn.target.value)) if defn.target.mode == "rr" else ref * Decimal(str(defn.target.value)) / 100 if defn.target.mode == "percent" else None
        if not td or td <= 0:
            continue
        sign = 1 if side == "buy" else -1
        try:
            out.append(DhanOrderIntent(symbol=inst.symbol, security_id=security_id, side=side, quantity=ft.quantity, strategy_key=ft.strategy_key,
                                       signal_id=f"{ft.id}:{signal_time.isoformat()}:{inst.symbol}:{side}", signal_time=signal_time, reference_price=ref,
                                       stop_price=_round_tick(ref - sign * sd), target_price=_round_tick(ref + sign * td)))
        except ValueError as error:
            logger.info("forward_test_intent_rejected symbol=%s reason=%s", inst.symbol, error)
    return out


# ───────────────────────── one pass ─────────────────────────
def run_once(db: Session, ft: ForwardTest, now: datetime, *, client_factory: ClientFactory | None = None, refresh: bool = True,
             refresher: Callable[..., dict[UUID, str | None]] | None = None) -> dict[str, Any]:
    """Refresh data, replay, snapshot; then (live tests only) hand fresh intents to the guarded execution layer."""
    user = db.get(User, ft.user_id)
    instruments = instruments_of(db, ft)
    errors: dict[UUID, str | None] = {}
    try:
        # a whole-list test is fed by the background rotation (master_bar_refresh), not refreshed inline
        if refresh and user is not None and not uses_master(ft) and (session_open(now) or now - (aware(ft.last_run_at) or now - timedelta(days=1)) > timedelta(hours=6)):
            errors = (refresher or refresh_bars)(db, user, instruments, now)
        snapshot, runs = replay(db, ft, instruments, now, errors)
        placed: list[dict[str, Any]] = []
        if ft.execute_live and client_factory is not None and session_open(now) and now >= start_of(ft.start_date):
            for run in runs:
                inst = next((i for i in instruments if str(i.id) == run.instrument_id), None)
                if inst is None or not len(run.frame):
                    continue
                last_close = (run.frame["time"].iloc[-1] + MINUTE).to_pydatetime()
                for intent in detect_intents(db, ft, inst, run, now):
                    row = attempt_order(db, ft.user_id, ft, inst, intent, now=now, last_bar_close=last_close, last_price=Decimal(repr(float(run.frame["close"].iloc[-1]))),
                                        client_factory=client_factory)
                    if row is not None:
                        placed.append({"symbol": row.symbol, "side": row.side, "status": row.status, "reason": row.block_reason, "order_id": row.dhan_order_id})
        snapshot["live_orders_this_pass"] = placed
        ft.snapshot, ft.last_error = snapshot, None
    except Exception as error:  # noqa: BLE001 - one broken test must not stop the others
        db.rollback()
        ft = db.get(ForwardTest, ft.id)
        ft.last_error = f"{type(error).__name__}: {str(error)[:200]}"
        logger.exception("forward_test_run_failed id=%s", ft.id)
    ft.last_run_at, ft.updated_at = now, now
    db.commit()
    return {"id": str(ft.id), "error": ft.last_error}


def build_client(db: Session, user_id: UUID) -> DhanOrderClient:
    """Dhan order client from the user's EXISTING stored Dhan token (no new credential storage)."""
    from app.api import market_data as md
    from app.broker_adapters import DhanAdapter

    adapter = DhanAdapter()
    tokens = md._fresh_dhan_tokens_worker(user_id, adapter)
    return DhanOrderClient(adapter.api_root, tokens["access_token"], tokens["client_id"])
