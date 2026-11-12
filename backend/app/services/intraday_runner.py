"""Glue between stored data, strategy definitions, the backtester and live signal evaluation.

Live signals and backtests share the SAME code path (``evaluate`` / ``evaluate_python`` over clean 1-minute bars), so an indicator value
seen live is the value a backtest would have used at that bar.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import numpy as np
import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Instrument, MarketBar
from app.models.strategies import IntradaySignal, Strategy, StrategyVersion
from app.services import signal_lifecycle as lifecycle
from app.services import intraday_indicators as ti
from app.services import intraday_validation as iv
from app.services.bar_quality import DEFAULT_REFUSE_MISSING_PCT, clean_and_validate
from app.services.provider_coverage import describe as describe_coverage, unserved_ranges
from app.services.intraday_backtest import BacktestConfig, InstrumentRun, dataset_hash, run_backtest
from app.services.intraday_python import SandboxError, evaluate_python
from app.services.intraday_strategy import StrategyDefinition, definition_hash, evaluate, explain, rules, validate_definition
from app.services import intraday_options as iopt
from app.services.option_selector import select_contract
from app.services.options_data import DbOptionData, coverage_report
from app.services.timeframes import MINUTE, UnsupportedMarket, minutes_of, profile_for

DEFAULT_WINDOW_DAYS = 30
MAX_WINDOW_DAYS = 800
LIVE_STALE_MINUTES = {"equity_in": 10, "crypto": 5}
# Live scans use the same missing-candle limit as backtests (5%): a signal is never shown as clean on data a backtest of the same window would refuse.
LIVE_MAX_MISSING_PCT = DEFAULT_REFUSE_MISSING_PCT


def freshness_view(stored: dict[str, Any] | None, now: datetime) -> dict[str, Any]:
    """The ONLY stale/fresh implementation. Age is derived from the stored as-of timestamp (last candle close) at read time,
    never from an age frozen at scan time. Missing/unparseable inputs give state "unknown", which callers must treat as NOT fresh."""
    stored = dict(stored or {})
    stored.pop("age_minutes", None)  # legacy rows froze an age at scan time; it is never trusted
    as_of, limit = stored.get("last_bar_close"), stored.get("stale_after_minutes")
    age = state = None
    try:
        if not as_of or limit is None:
            raise ValueError("missing as-of or limit")
        ts = pd.Timestamp(as_of)
        if pd.isna(ts):
            raise ValueError("unparseable as-of")
        ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts
        age = round(max(0.0, (pd.Timestamp(now) - ts).total_seconds() / 60), 1)
        state = "stale" if age > float(limit) else "fresh"
    except (TypeError, ValueError):
        age, state = None, "unknown"
    return {**stored, "age_minutes": age, "state": state}


def spot_gate(db: Session, spot_id: str, now: datetime) -> tuple[float | None, str | None, str | None]:
    """Quality gate for an option underlying's spot series: real (non-synthetic) bars, fresh, and under the missing-candle limit.
    Returns (last close, None, None) or (None, reason, rejection reason_code)."""
    inst = db.get(Instrument, UUID(spot_id))
    if inst is None:
        return None, "underlying spot instrument not found", "other"
    try:
        prof = profile_for(inst)
    except UnsupportedMarket:
        return None, f"underlying spot {inst.symbol}: unsupported market", "other"
    raw = load_bars(db, inst.id, None, now, tail=60)  # load_bars excludes synthetic bars
    clean, q = clean_and_validate(raw, prof, now=pd.Timestamp(now), max_missing_pct=LIVE_MAX_MISSING_PCT, unserved=unserved_ranges(db, inst.id))
    if len(clean) == 0:
        return None, f"underlying spot {inst.symbol}: no real 1-minute data", "no_history"
    age = (pd.Timestamp(now) - (clean["time"].iloc[-1] + MINUTE)).total_seconds() / 60
    if age > LIVE_STALE_MINUTES[prof.key]:
        return None, f"underlying spot {inst.symbol} is stale ({round(age)} min old)", "stale_spot"
    if q.get("status") == "refuse":
        return None, f"underlying spot {inst.symbol}: 1-minute data quality is insufficient", "missing_data_limit"
    return float(clean["close"].iloc[-1]), None, None


class RunnerError(ValueError):
    pass


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def load_bars(db: Session, instrument_id: UUID, start: datetime | None, end: datetime | None, *, tail: int | None = None) -> pd.DataFrame:
    """Stored non-synthetic 1-minute bars (open-time stamped) as a float frame. Synthetic bars are never used as market evidence.
    A FUTURES instrument registered from the derivatives platform reads its stored contract candles (futures_candles_1m)."""
    inst = db.get(Instrument, instrument_id)
    if inst is not None and inst.asset_class == "future":
        from app.models import FuturesCandle1m
        from app.services.derivatives_ingest import futures_contract_for_instrument
        contract = futures_contract_for_instrument(db, instrument_id)
        if contract is None:
            return pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume"])
        fq = select(FuturesCandle1m.ts, FuturesCandle1m.open, FuturesCandle1m.high, FuturesCandle1m.low, FuturesCandle1m.close, FuturesCandle1m.volume).where(FuturesCandle1m.contract_id == contract.id)
        if start is not None:
            fq = fq.where(FuturesCandle1m.ts >= start)
        if end is not None:
            fq = fq.where(FuturesCandle1m.ts < end)
        frows = db.execute(fq.order_by(FuturesCandle1m.ts.desc() if tail else FuturesCandle1m.ts).limit(tail) if tail else fq.order_by(FuturesCandle1m.ts)).all()
        if tail:
            frows = frows[::-1]
        if not frows:
            return pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume"])
        return pd.DataFrame({"time": pd.to_datetime([r[0] for r in frows], utc=True), "open": [float(r[1]) for r in frows], "high": [float(r[2]) for r in frows], "low": [float(r[3]) for r in frows],
                             "close": [float(r[4]) for r in frows], "volume": [float(r[5]) for r in frows]})
    q = select(MarketBar.timestamp, MarketBar.open, MarketBar.high, MarketBar.low, MarketBar.close, MarketBar.volume).where(
        MarketBar.instrument_id == instrument_id, MarketBar.interval == "1m", MarketBar.is_synthetic.is_(False))
    if start is not None:
        q = q.where(MarketBar.timestamp >= start)
    if end is not None:
        q = q.where(MarketBar.timestamp < end)
    q = q.order_by(MarketBar.timestamp.desc() if tail else MarketBar.timestamp)
    if tail:
        q = q.limit(tail)
    rows = db.execute(q).all()
    if tail:
        rows = rows[::-1]
    if not rows:
        return pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume"])
    return pd.DataFrame({"time": pd.to_datetime([r[0] for r in rows], utc=True), "open": [float(r[1]) for r in rows], "high": [float(r[2]) for r in rows],
                         "low": [float(r[3]) for r in rows], "close": [float(r[4]) for r in rows], "volume": [float(r[5]) for r in rows]})


def window(cfg: BacktestConfig, now: datetime) -> tuple[datetime, datetime]:
    end = _utc(cfg.end) or now
    start = _utc(cfg.start) or end - timedelta(days=DEFAULT_WINDOW_DAYS)
    if end <= start:
        raise RunnerError("end must be after start")
    if (end - start).days > MAX_WINDOW_DAYS:
        raise RunnerError(f"window longer than {MAX_WINDOW_DAYS} days")
    return start, end


def resolve_instruments(db: Session, defn: StrategyDefinition, ids: list[str] | None) -> list[Instrument]:
    wanted = ids or defn.universe
    if not wanted:
        raise RunnerError("choose at least one instrument")
    out = []
    for raw in wanted:
        inst = db.get(Instrument, UUID(str(raw)))
        if inst is None:
            raise RunnerError(f"instrument {raw} not found")
        try:
            prof = profile_for(inst)
        except UnsupportedMarket as e:
            raise RunnerError(str(e)) from None
        if prof.key != defn.market:
            raise RunnerError(f"{inst.symbol} is a {prof.key} instrument but the strategy market is {defn.market}")
        out.append(inst)
    return out


def evaluate_any(defn: StrategyDefinition, source_text: str | None, frame: pd.DataFrame, profile) -> Any:
    if defn.python is not None:
        if not source_text:
            raise RunnerError("Python source for this version is missing")
        return evaluate_python(defn, source_text, frame, profile)
    return evaluate(defn, frame, profile)


def make_backtester(source_text: str | None):
    def bt(defn: StrategyDefinition, runs: list[InstrumentRun], cfg: BacktestConfig) -> dict[str, Any]:
        fresh = [InstrumentRun(r.instrument_id, r.symbol, r.profile, r.frame, evaluate_any(defn, source_text, r.frame, r.profile), r.quality) for r in runs if len(r.frame)]
        return run_backtest(defn, fresh, cfg)
    return bt


def prepare_runs(db: Session, defn: StrategyDefinition, instruments: list[Instrument], cfg: BacktestConfig, now: datetime) -> tuple[list[InstrumentRun], list[dict[str, Any]]]:
    """Clean + validate each instrument's 1-minute data. Instruments that fail validation are refused and reported, never patched."""
    start, end = window(cfg, now)
    runs: list[InstrumentRun] = []
    refused: list[dict[str, Any]] = []
    for inst in instruments:
        prof = profile_for(inst)
        raw = load_bars(db, inst.id, start, end)
        clean, quality = clean_and_validate(raw, prof, now=pd.Timestamp(now), max_missing_pct=cfg.max_missing_pct, unserved=unserved_ranges(db, inst.id))
        quality = {**quality, "symbol": inst.symbol, "instrument_id": str(inst.id), "source": "stored 1m bars (non-synthetic)"}
        if quality["status"] == "refuse":
            refused.append({"instrument_id": str(inst.id), "symbol": inst.symbol, "reason": "; ".join(quality.get("issues", [])) or "insufficient data", "quality": quality})
            continue
        runs.append(InstrumentRun(str(inst.id), inst.symbol, prof, clean, None, quality))  # type: ignore[arg-type]
    return runs, refused


def options_blockers(db: Session, defn: StrategyDefinition, now: datetime) -> tuple[dict[str, Any], list[str]]:
    """Coverage check for an options strategy. Returns (coverage, blockers); an empty blocker list means the requested price mode can run on real data."""
    cov = coverage_report(db, now, defn.options.underlying.underlying)
    blockers = list(cov["blockers"])
    want = defn.options.execution.price_mode
    if cov["backtest_mode"] == "unavailable":
        return cov, blockers or ["required options datasets are missing"]
    if want == "quote_accurate" and cov["backtest_mode"] != "quote_accurate":
        blockers.append("This strategy requires quote-accurate execution but only option candles are stored. Switch price mode to indicative OHLC explicitly to run a clearly-labelled indicative test.")
    if want == "indicative_ohlc" and cov["datasets"]["option_candles_1m"]["status"] != "available":
        blockers.append("Indicative OHLC mode needs stored option 1-minute candles; none exist.")
    return cov, blockers


def execute_options(db: Session, defn: StrategyDefinition, kind: str, instruments: list[Instrument], cfg: BacktestConfig, options: dict[str, Any], now: datetime) -> dict[str, Any]:
    cov, blockers = options_blockers(db, defn, now)
    if blockers:
        raise RunnerError("options data unavailable: " + " | ".join(blockers))
    runs, refused = prepare_runs(db, defn, instruments, cfg, now)
    if not runs:
        raise RunnerError("no signal instrument has sufficient clean 1-minute data: " + "; ".join(f"{r['symbol']}: {r['reason']}" for r in refused))
    run = runs[0]
    spot = None
    sid = defn.options.underlying.spot_instrument_id
    if sid:
        start, end = window(cfg, now)
        spot = load_bars(db, UUID(sid), start, end)
    res = evaluate(defn, run.frame, run.profile)
    src = DbOptionData(db)
    if kind == "single":
        d = iopt.run_options_backtest(defn, run.frame, res, run.profile, src, initial_capital=cfg.initial_capital, spot_frame=spot, symbol=run.symbol, instrument_id=run.instrument_id, direction=cfg.direction)
    else:
        k = {"is_oos": "is_oos", "sensitivity": "sensitivity", "walk_forward": "walk_forward"}[kind]
        d = iopt.run_validation(k, defn, run.frame, res, run.profile, src, initial_capital=cfg.initial_capital, spot_frame=spot,
                                train_days=int(options.get("train_days", 10)), test_days=int(options.get("test_days", 3)), split=float(options.get("split_ratio", 0.7)))
    d["dataset"] = {"sha256": dataset_hash([run]), "instruments": [{"instrument_id": run.instrument_id, "symbol": run.symbol, "bars": int(len(run.frame)), "quality": run.quality,
                                                                    "start": run.frame["time"].iloc[0].isoformat(), "end": run.frame["time"].iloc[-1].isoformat()}]}
    d["options_coverage"] = cov
    d["refused_instruments"] = refused
    d["data_window"] = {"start": window(cfg, now)[0].isoformat(), "end": window(cfg, now)[1].isoformat()}
    return d


def execute(db: Session, defn: StrategyDefinition, source_text: str | None, kind: str, instruments: list[Instrument], cfg: BacktestConfig,
            options: dict[str, Any], now: datetime) -> dict[str, Any]:
    from app.services.engine_strategies import is_engine
    if is_engine(defn):
        from app.services.scalp_hold_runner import execute_scalp_hold
        return execute_scalp_hold(db, defn, kind, instruments, cfg, window(cfg, now), options)
    if defn.options is not None:
        return execute_options(db, defn, kind, instruments, cfg, options, now)
    runs, refused = prepare_runs(db, defn, instruments, cfg, now)
    if not runs:
        raise RunnerError("no instrument has sufficient clean 1-minute data: " + "; ".join(f"{r['symbol']}: {r['reason']}" for r in refused))
    bt = make_backtester(source_text)
    if kind == "single":
        result = bt(defn, runs, cfg)
    elif kind == "is_oos":
        result = iv.in_out_of_sample(defn, runs, cfg, bt, float(options.get("split_ratio", 0.7)))
    elif kind == "sensitivity":
        result = iv.sensitivity(defn, runs, cfg, bt, options.get("parameters"), int(options.get("steps", 1)))
    elif kind == "walk_forward":
        result = iv.walk_forward(defn, runs, cfg, bt, int(options.get("train_days", 20)), int(options.get("test_days", 5)), options.get("parameters"))
    else:
        raise RunnerError(f"unknown backtest kind {kind!r}")
    result["refused_instruments"] = refused
    result["data_window"] = {"start": window(cfg, now)[0].isoformat(), "end": window(cfg, now)[1].isoformat()}
    return result


# ───────────────────────── live signals ─────────────────────────
def _needed_bars(defn: StrategyDefinition) -> int:
    _, req = validate_definition(defn)
    need = (req.warmup_base_minutes if req else 500) + 3 * minutes_of(defn.decision_timeframe) + 120
    return int(min(max(need * 2, 600), 40_000))


def _expiry(defn: StrategyDefinition, signal_time: datetime) -> datetime:
    return signal_time + timedelta(minutes=3 * minutes_of(defn.decision_timeframe))


def scan_instrument(db: Session, strategy: Strategy, version: StrategyVersion, defn: StrategyDefinition, inst: Instrument, now: datetime,
                    scan_run_id: UUID | None = None) -> list[IntradaySignal]:
    """Evaluate one strategy version on one instrument as of ``now`` and persist new/updated signals. Deduplicated per decision candle.
    Candidates that are suppressed never become signals: each writes a ScanRejection with an enumerated reason."""
    run_id = scan_run_id or uuid4()
    source = f"scan:{run_id}"
    prof = profile_for(inst)
    raw = load_bars(db, inst.id, None, now, tail=_needed_bars(defn))
    source_text = (version.source_artifact or {}).get("text")
    out: list[IntradaySignal] = []

    def reject(code: str, detail: str, direction: str | None, observed: datetime | None, payload: dict | None = None) -> None:
        lifecycle.record_rejection(db, user_id=strategy.user_id, scan_run_id=run_id, strategy_id=strategy.id, strategy_version_id=version.id, instrument_id=inst.id,
                                   direction=direction, reason_code=code, recorded_at=now, observed_data_time=observed, reason_detail=detail, payload=payload)

    def record(direction: str, signal_time: datetime, price: float, status: str, reason: str | None, code: str | None, met: list, unmet: list, indicators: dict, fresh: dict,
               text: str, option: dict | None = None):
        key = f"{defn.decision_timeframe}:{direction}:{signal_time.isoformat()}:{inst.id}"
        existing = db.scalar(select(IntradaySignal).where(IntradaySignal.strategy_id == strategy.id, IntradaySignal.dedupe_key == key))
        if existing is not None:
            return existing  # same decision candle already recorded: no new signal, no rejection
        if status == "suppressed":
            reject(code or "other", reason or "suppressed", direction, signal_time, {"signal_time": signal_time.isoformat(), "reference_price": str(Decimal(repr(price))),
                                                                                   "data_freshness": fresh, "option": option})
            return None
        row = IntradaySignal(
            user_id=strategy.user_id, strategy_id=strategy.id, strategy_version=version.version, instrument_id=inst.id, market=defn.market, direction=direction,
            status="ready", suppression_reason=None, timeframe=defn.decision_timeframe, signal_time=signal_time, reference_price=Decimal(repr(price)),
            indicators=indicators, conditions_met=met, conditions_unmet=unmet, rationale=text[:500], data_freshness=fresh, dedupe_key=key,
            expires_at=_expiry(defn, signal_time), created_at=now, updated_at=now, option=option)
        lifecycle.create_signal(db, row, state="ready", observed_data_time=signal_time, event_time=now, source=source, strategy_version_id=version.id,
                                payload={"data_freshness": fresh})
        return row

    clean, quality = clean_and_validate(raw, prof, now=pd.Timestamp(now), max_missing_pct=LIVE_MAX_MISSING_PCT, unserved=unserved_ranges(db, inst.id))
    refresh_lifecycle(db, strategy, inst, defn, clean, prof, source_text, now, source=source)
    if len(clean) == 0:
        reject("no_history", f"{inst.symbol}: no usable stored 1-minute bars", None, None, {"rows_loaded": int(len(raw)), "quality": {k: quality.get(k) for k in ("status", "issues")}})
        return out
    last_close = clean["time"].iloc[-1] + MINUTE
    age_min = (pd.Timestamp(now) - last_close).total_seconds() / 60
    fresh = {"last_bar_close": last_close.isoformat(), "stale_after_minutes": LIVE_STALE_MINUTES[prof.key],
             "bars_used": int(len(clean)), "quality_status": quality.get("status"), "missing_pct": quality.get("missing_pct")}
    try:
        res = evaluate_any(defn, source_text, clean, prof)
    except (SandboxError, RunnerError, ValueError) as e:
        raise RunnerError(f"{inst.symbol}: {e}") from None
    if len(res.decision_idx) == 0:
        reject("insufficient_bars", f"{inst.symbol}: {len(clean)} clean 1-minute bars give no completed {defn.decision_timeframe} decision candle", None, last_close.to_pydatetime(),
               {"bars_used": int(len(clean))})
        return out
    last = int(res.decision_idx[-1])
    signal_time = (clean["time"].iloc[last] + MINUTE).to_pydatetime()
    known = clean.loc[clean["time"] < pd.Timestamp(signal_time)]  # only 1m bars the decision could see; aggregate() drops candles still forming at the end of this frame
    for rule, direction, allowed in (("long_entry", "long", defn.constraints.allow_long), ("short_entry", "short", defn.constraints.allow_short)):
        if not res.signals[rule][last]:
            continue
        details = explain(res, rule, last)
        met = [d for d in details if d["satisfied"]]
        unmet = [d for d in details if not d["satisfied"]]
        inds = {a.alias: {o: (None if np.isnan(v) else round(float(v), 6)) for o, v in _latest(defn, known, prof, a).items()} for a in defn.indicators}
        price = float(clean["close"].iloc[last])
        text = f"{direction.upper()} on the {defn.decision_timeframe} candle closing {signal_time.isoformat()}: " + "; ".join(d["label"] for d in met if d["kind"] != "group")
        reason = None
        code = None
        status = "ready"
        if not allowed:
            status, reason, code = "suppressed", f"{direction} trades are disabled for this strategy", "other"
        elif age_min > LIVE_STALE_MINUTES[prof.key]:
            status, reason, code = "suppressed", f"data is stale ({round(age_min)} min old)", "stale_data"
        elif quality.get("status") == "refuse":
            code = "missing_data_limit"
            status, reason =  "suppressed", f"1-minute data quality is insufficient (missing share {quality.get('missing_pct')}% > {LIVE_MAX_MISSING_PCT}% limit)"
        else:
            active = db.scalar(select(IntradaySignal).where(IntradaySignal.strategy_id == strategy.id, IntradaySignal.instrument_id == inst.id,
                                                            IntradaySignal.direction == direction, IntradaySignal.status.in_(lifecycle.OPEN_STATES),
                                                            IntradaySignal.expires_at > now))
            if active is not None:
                status, reason, code = "suppressed", "an earlier signal in this direction is still active", "other"
        option = None
        if defn.options is not None:
            oex = defn.options.execution
            ref, label = Decimal(repr(price)), "futures price (proxy: includes basis)"
            sid = defn.options.underlying.spot_instrument_id
            spot_reason = None
            if sid:
                spot_close, spot_reason, spot_code = spot_gate(db, sid, now)
                if spot_close is not None:
                    ref, label = Decimal(repr(spot_close)), "spot index"
            if spot_reason:  # bad spot must never drive strike selection
                option = {"ok": False, "no_trade_reasons": [spot_reason], "candidates": [], "mode": "decision_support_only"}
                if status == "ready":
                    status, reason, code = "suppressed", "NO TRADE: " + spot_reason, spot_code
                row = record(direction, signal_time, price, status, reason, code, met, unmet, inds, fresh, text + " | option overlay suppressed: " + spot_reason, option)
                if row is not None:
                    out.append(row)
                continue
            sel =select_contract(DbOptionData(db), iopt.selection_config(defn), underlying=defn.options.underlying.underlying, direction=direction, signal_time=now, reference_price=ref, reference_label=label)
            option = sel.as_dict()
            option["mode"] = "decision_support_only"
            if sel.ok and sel.quote is not None and sel.quote.ask is not None:
                ask = sel.quote.ask
                option["entry_reference_ask"] = str(ask)
                option["premium_stop"] = str((ask * (1 - Decimal(repr(oex.premium_stop_pct)) / 100)).quantize(Decimal("0.05")))
                option["premium_target"] = str((ask * (1 + Decimal(repr(oex.premium_stop_pct * oex.target_r)) / 100)).quantize(Decimal("0.05")))
                option["max_hold_minutes"] = oex.max_hold_minutes
            elif status == "ready":
                status, code = "suppressed", "other"
                reason = "NO TRADE: " + (sel.no_trade_reasons[0] if sel.no_trade_reasons else "no eligible option contract")
            text += " | " + (sel.why or "")
        row = record(direction, signal_time, price, status, reason, code, met, unmet, inds, fresh, text, option)
        if row is not None:
            out.append(row)
    return out


def _latest(defn: StrategyDefinition, clean: pd.DataFrame, prof, decl) -> dict[str, float]:
    f = clean
    from app.services.timeframes import aggregate
    frame = aggregate(f, decl.timeframe, prof)
    if frame.empty:
        return {o: float("nan") for o in ti.REGISTRY[decl.indicator].outputs}
    comp = ti.compute(decl.indicator, frame, decl.params, ti.Ctx(prof, decl.timeframe))
    return {o: float(v[-1]) for o, v in comp.items()}


def refresh_lifecycle(db: Session, strategy: Strategy, inst: Instrument, defn: StrategyDefinition, clean: pd.DataFrame, prof, source_text: str | None, now: datetime,
                      source: str = "scan") -> None:
    """ready -> active on the next scan; open signals -> expired after expiry; -> invalidated when the strategy's exit condition fires after the signal.
    Every change goes through signal_lifecycle.transition_signal (append-only event + current state)."""
    open_rows = list(db.scalars(select(IntradaySignal).where(IntradaySignal.strategy_id == strategy.id, IntradaySignal.instrument_id == inst.id,
                                                              IntradaySignal.status.in_(lifecycle.OPEN_STATES))))
    if not open_rows or len(clean) == 0:
        return
    latest_data_time = (clean["time"].iloc[-1] + MINUTE).to_pydatetime()  # close of the newest stored candle: the data this scan observed
    res = None
    for row in open_rows:
        st = _utc(row.signal_time)
        if _utc(row.expires_at) <= now:
            # expiry is clock-driven: the market-time boundary crossed is expires_at; the newest data seen is in the payload
            lifecycle.transition_signal(db, row, "expired", reason_code="time_expired", observed_data_time=_utc(row.expires_at), event_time=now, source=source,
                                        payload={"latest_stored_candle_close": latest_data_time.isoformat()})
            continue
        if res is None:
            try:
                res = evaluate_any(defn, source_text, clean, prof)
            except Exception:  # noqa: BLE001 - lifecycle refresh must never block a scan
                res = False
        if res:
            idx = res.decision_idx
            times = clean["time"].iloc[idx] + MINUTE if len(idx) else None
            if times is not None:
                later = np.flatnonzero((times > pd.Timestamp(st)).to_numpy())
                exit_rule = "long_exit" if row.direction == "long" else "short_exit"
                opposite = "short_entry" if row.direction == "long" else "long_entry"
                hit = next(((j, rule) for j in later for rule in (exit_rule, opposite) if res.signals[rule][idx[j]]), None)
                if hit is not None:
                    j, rule = hit
                    lifecycle.transition_signal(db, row, "invalidated", reason_code="exit_condition" if rule == exit_rule else "opposite_signal",
                                                observed_data_time=times.iloc[j].to_pydatetime(), event_time=now, source=source, payload={"rule": rule})
                    continue
        if row.status == "ready" and _utc(row.created_at) < now:
            lifecycle.transition_signal(db, row, "active", reason_code="confirmed_next_scan", observed_data_time=latest_data_time, event_time=now, source=source)


def data_health(db: Session, instruments: list[Instrument], now: datetime) -> list[dict[str, Any]]:
    out = []
    for inst in instruments:
        try:
            prof = profile_for(inst)
        except UnsupportedMarket:
            continue
        raw = load_bars(db, inst.id, now - timedelta(days=5), now)
        totals = db.execute(select(func.count(MarketBar.id), func.min(MarketBar.timestamp), func.max(MarketBar.timestamp)).where(
            MarketBar.instrument_id == inst.id, MarketBar.interval == "1m", MarketBar.is_synthetic.is_(False))).one()
        total = {"total_bars": int(totals[0] or 0), "history_start": totals[1].isoformat() if totals[1] else None,
                 "history_end": totals[2].isoformat() if totals[2] else None, "window_days": 5}
        if raw.empty:
            out.append({"instrument_id": str(inst.id), "symbol": inst.symbol, "market": prof.key, "status": "no_data", **total,
                        "issues": ["no 1-minute bars in the last 5 days" + (f" (total stored history: {total['total_bars']} bars)" if total["total_bars"] else "")]})
            continue
        _, q = clean_and_validate(raw, prof, now=pd.Timestamp(now), unserved=unserved_ranges(db, inst.id))
        out.append({"instrument_id": str(inst.id), "symbol": inst.symbol, "market": prof.key, "status": q["status"], "bars": q["bars"], **total,
                    "last_bar_age_seconds": q.get("last_bar_age_seconds"), "missing_pct": q.get("missing_pct"), "issues": q.get("issues", []),
                    "provider_unserved": q.get("provider_unserved"), "provider_coverage": describe_coverage(db, inst.id)})
    return out
