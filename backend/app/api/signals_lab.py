"""Signals workspace API: intraday strategy definitions, imports, backtests and live signals. Decision support only: no orders, no execution."""
from __future__ import annotations

import hashlib
from datetime import date, datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.auth import require_csrf
from app.api.strategies import require_verified_session
from app.db import get_db
from app.models import Instrument, MarketBar, UserSession
from app.models.jobs import Job, ScanRun, ScannerSchedule, ScannerSettings
from app.models.strategies import BacktestRun, IntradaySignal, IntradaySignalEvent, ScanRejection, Strategy, StrategyVersion
from app.services import transaction_costs as costs
from app.services import intraday_indicators as ti
from app.services import intraday_python as ipy
from app.services import intraday_runner as runner
from app.services import job_queue, scan_scheduler
from app.services import signal_lifecycle as lifecycle
from app.services.options_data import coverage_report
from app.services.options_strategy import build_underlying_rules
from app.services.intraday_strategy import OptionsUnderlying
from app.services.bar_quality import clean_and_validate
from app.services.intraday_backtest import BacktestConfig
from app.services.intraday_strategy import (ENGINE_VERSION, SCHEMA_VERSION, StrategyDefinition, definition_hash, tunable_parameters, validate_definition)
from app.services.pine_import import SUPPORT_MATRIX, import_pine
from app.services.starter_strategies import LABEL as STARTER_LABEL, STARTERS
from app.services.timeframes import TIMEFRAME_MINUTES, UnsupportedMarket, profile_for

router = APIRouter(prefix="/signals-lab", tags=["signals-lab"])
DISCLAIMER = "Decision support, not investment advice. Historical statistics only; no orders are placed."
MAX_SOURCE_CHARS = 200_000


def _now() -> datetime:
    return datetime.now(timezone.utc)


class DefinitionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    definition: dict[str, Any]
    source_text: str | None = Field(default=None, max_length=MAX_SOURCE_CHARS)


class ImportBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    filename: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=MAX_SOURCE_CHARS)
    market: str = Field(default="equity_in", pattern="^(equity_in|crypto)$")
    decision_timeframe: str = "5m"


class BacktestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    strategy_id: UUID
    version: int | None = Field(default=None, ge=1)
    kind: str = Field(default="single", pattern="^(single|is_oos|walk_forward|sensitivity)$")
    instruments: list[UUID] = Field(default_factory=list, max_length=50)
    config: dict[str, Any] = Field(default_factory=dict)
    options: dict[str, Any] = Field(default_factory=dict)


class DuplicateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)


class ScanBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    strategy_ids: list[UUID] = Field(default_factory=list, max_length=50)


# ───────────────────────── helpers ─────────────────────────
def _parse_definition(raw: dict[str, Any]) -> StrategyDefinition:
    try:
        defn = StrategyDefinition.model_validate(raw)
    except ValidationError as e:
        raise HTTPException(422, detail={"code": "invalid_definition", "errors": [{"where": ".".join(map(str, x["loc"])), "message": x["msg"]} for x in e.errors()][:30]}) from None
    if defn.schema_version != SCHEMA_VERSION:
        raise HTTPException(422, detail={"code": "unsupported_schema", "message": f"schema_version must be {SCHEMA_VERSION}"})
    return defn


def _check(defn: StrategyDefinition) -> None:
    issues, req = validate_definition(defn)
    if req is None:
        raise HTTPException(422, detail={"code": "invalid_definition", "errors": [i for i in issues if i["level"] == "error"]})


def _owned(db: Session, user_id: UUID, strategy_id: UUID) -> Strategy:
    s = db.scalar(select(Strategy).where(Strategy.id == strategy_id, Strategy.user_id == user_id))
    if s is None:
        raise HTTPException(404, "Strategy not found")
    return s


def _version(db: Session, strategy: Strategy, version: int | None = None) -> StrategyVersion:
    v = db.scalar(select(StrategyVersion).where(StrategyVersion.strategy_id == strategy.id, StrategyVersion.version == (version or strategy.current_version)))
    if v is None:
        raise HTTPException(404, "Strategy version not found")
    return v


def _anchor_instrument(db: Session, defn: StrategyDefinition) -> Instrument:
    if not defn.universe:
        raise HTTPException(422, detail={"code": "invalid_definition", "errors": [{"level": "error", "where": "universe", "message": "choose at least one instrument"}]})
    try:
        insts = runner.resolve_instruments(db, defn, None)
    except runner.RunnerError as e:
        raise HTTPException(422, detail={"code": "invalid_universe", "message": str(e)}) from None
    return insts[0]


def _new_version_row(defn: StrategyDefinition, strategy_id: UUID, version: int, source_text: str | None, now: datetime) -> StrategyVersion:
    artifact = None
    if defn.source.artifact is not None:
        if source_text is None:
            raise HTTPException(422, detail={"code": "source_missing", "message": "the original source text must accompany an imported strategy"})
        if hashlib.sha256(source_text.encode()).hexdigest() != defn.source.artifact.get("sha256"):
            raise HTTPException(422, detail={"code": "source_mismatch", "message": "source text does not match the recorded sha256"})
        artifact = {**defn.source.artifact, "text": source_text}
    if defn.python is not None:
        if artifact is None or hashlib.sha256(source_text.encode()).hexdigest() != defn.python.source_sha256:  # type: ignore[union-attr]
            raise HTTPException(422, detail={"code": "source_mismatch", "message": "python source hash does not match the definition"})
        errors = ipy.screen_source(source_text)  # type: ignore[arg-type]
        if errors:
            raise HTTPException(422, detail={"code": "python_rejected", "errors": errors})
    dumped = defn.model_dump(mode="json")
    return StrategyVersion(strategy_id=strategy_id, version=version, params={"strategy_id": "intraday", "schema": SCHEMA_VERSION, "definition_hash": definition_hash(dumped)},
                           created_at=now, schema_version=SCHEMA_VERSION, source_type=defn.source.type, definition=dumped, definition_hash=definition_hash(dumped),
                           source_artifact=artifact)


def _summary_of(run: BacktestRun, with_result: bool = False) -> dict[str, Any]:
    res = run.result or {}
    out = {"id": str(run.id), "strategy_id": str(run.strategy_id), "strategy_version": run.strategy_version, "kind": run.kind, "status": run.status,
           "progress": run.progress, "created_at": run.created_at.isoformat(), "finished_at": run.finished_at.isoformat() if run.finished_at else None,
           "error": run.error, "engine_version": run.engine_version, "indicator_version": run.indicator_version, "definition_hash": run.definition_hash,
           "config": run.config, "metrics": res.get("metrics"), "headline": {k: res.get(k) for k in ("in_sample", "out_of_sample", "oos_total_trades", "oos_net_profit") if k in res},
           "disclaimer": DISCLAIMER}
    if with_result:
        out["result"] = res
        out["definition_snapshot"] = run.definition_snapshot
        out["provenance"] = run.provenance
    return out


# ───────────────────────── meta / data ─────────────────────────
@router.get("/meta")
def meta(_: UserSession = Depends(require_verified_session)) -> dict[str, Any]:
    from app.services.intraday_strategy import COMPARE_OPS, PRICE_FIELDS
    return {
        "schema_version": SCHEMA_VERSION, "engine_version": ENGINE_VERSION, "indicator_library_version": ti.INDICATOR_LIBRARY_VERSION,
        "timeframes": list(TIMEFRAME_MINUTES), "markets": ["equity_in", "crypto"], "price_fields": list(PRICE_FIELDS), "compare_ops": list(COMPARE_OPS),
        "indicators": ti.catalog(), "cost_profiles": [p.describe() for p in costs.SCHEDULES.values()],
        "pine": SUPPORT_MATRIX, "python": {"template": ipy.TEMPLATE, "allowed_imports": list(ipy.ALLOWED_IMPORTS), "timeout_seconds": ipy.DEFAULT_TIMEOUT,
                                           "memory_mb": ipy.DEFAULT_MEMORY // (1024 * 1024),
                                           "isolation": "separate interpreter + AST screen + scrubbed env + rlimits + wall-clock kill. Defence in depth for a single-user tool; not a container boundary."},
        "backtest_defaults": BacktestConfig().model_dump(mode="json"), "disclaimer": DISCLAIMER,
    }


@router.get("/instruments")
def instruments(market: str | None = Query(default=None, pattern="^(equity_in|crypto)$"), query: str = Query(default="", max_length=40),
                _: UserSession = Depends(require_verified_session), db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    """Instruments eligible for intraday signals, with how much stored 1-minute history each has."""
    stats = {r[0]: r for r in db.execute(select(MarketBar.instrument_id, func.count(), func.min(MarketBar.timestamp), func.max(MarketBar.timestamp))
                                         .where(MarketBar.interval == "1m", MarketBar.is_synthetic.is_(False)).group_by(MarketBar.instrument_id))}
    q = select(Instrument).where(Instrument.is_synthetic.is_(False))
    if query:
        q = q.where(Instrument.symbol.ilike(f"%{query}%") | Instrument.display_name.ilike(f"%{query}%"))
    rows = []
    for inst in db.scalars(q.order_by(Instrument.symbol).limit(2000)):
        try:
            prof = profile_for(inst)
        except UnsupportedMarket:
            continue
        if market and prof.key != market:
            continue
        s = stats.get(inst.id)
        if s is None and not query and prof.key == "equity_in":
            continue  # keep the picker usable: Indian equities only listed when they have 1m data, unless searched for
        rows.append({"id": str(inst.id), "symbol": inst.symbol, "name": inst.display_name, "market": prof.key, "currency": inst.currency,
                     "bars_1m": int(s[1]) if s else 0, "first_bar": s[2].isoformat() if s else None, "last_bar": s[3].isoformat() if s else None})
    return rows[:300]


@router.get("/data-quality/{instrument_id}")
def data_quality(instrument_id: UUID, days: int = Query(default=30, ge=1, le=800), _: UserSession = Depends(require_verified_session),
                 db: Session = Depends(get_db)) -> dict[str, Any]:
    inst = db.get(Instrument, instrument_id)
    if inst is None:
        raise HTTPException(404, "Instrument not found")
    try:
        prof = profile_for(inst)
    except UnsupportedMarket as e:
        raise HTTPException(422, str(e)) from None
    from datetime import timedelta
    now = _now()
    import pandas as pd
    raw = runner.load_bars(db, inst.id, now - timedelta(days=days), now)
    _, report = clean_and_validate(raw, prof, now=pd.Timestamp(now))
    return {**report, "symbol": inst.symbol, "source": "stored 1m bars (non-synthetic)"}


@router.get("/health")
def health(db: Session = Depends(get_db), active_session: UserSession = Depends(require_verified_session)) -> dict[str, Any]:
    """Dashboard data: strategy counts, recent signals/backtests and 1-minute data health of the instruments strategies use."""
    uid = active_session.user_id
    strategies = list(db.scalars(select(Strategy).where(Strategy.user_id == uid)))
    counts: dict[str, int] = {"total": len(strategies), "active": 0, "suspended": 0, "retired": 0}
    insts: dict[UUID, Instrument] = {}
    for s in strategies:
        counts[s.status] += 1
        v = _version(db, s)
        universe = (v.definition or {}).get("universe") or ([] if v.source_type == "starter" else [str(s.instrument_id)])
        for iid in universe:
            inst = db.get(Instrument, UUID(iid))
            if inst is not None:
                insts[inst.id] = inst
    sigs = db.scalars(select(IntradaySignal).where(IntradaySignal.user_id == uid).order_by(IntradaySignal.created_at.desc()).limit(8))
    runs = db.scalars(select(BacktestRun).where(BacktestRun.user_id == uid).order_by(BacktestRun.created_at.desc()).limit(8))
    return {"counts": counts, "recent_signals": [_signal_out(db, x) for x in sigs], "recent_backtests": [_summary_of(r) for r in runs],
            "data_health": runner.data_health(db, list(insts.values())[:30], _now()), "disclaimer": DISCLAIMER}


# ───────────────────────── strategies ─────────────────────────
def _summary(defn: dict[str, Any] | None) -> list[str]:
    if not defn:
        return []
    out = []
    for d in defn.get("indicators", []):
        params = ",".join(str(v) for v in ti.resolve_params(d["indicator"], d.get("params")).values() if not isinstance(v, str) or v)
        out.append(f"{d['indicator'].upper()}({params})@{d['timeframe']}")
    return out


_COVERAGE_CACHE: dict[str, Any] = {}


def _options_coverage_brief(db: Session, underlying: str) -> dict[str, Any]:
    now = _now()
    hit = _COVERAGE_CACHE.get(underlying)
    if hit and (now - hit[0]).total_seconds() < 5 and hit[2] == id(db.get_bind()):
        return hit[1]
    cov = coverage_report(db, now, underlying)
    brief = {"mode": cov["backtest_mode"], "ready": cov["backtest_ready"], "blockers": cov["blockers"][:4], "datasets": {k: v["status"] for k, v in cov["datasets"].items()}}
    _COVERAGE_CACHE[underlying] = (now, brief, id(db.get_bind()))
    return brief


def _strategy_out(db: Session, s: Strategy, full: bool = False) -> dict[str, Any]:
    v = _version(db, s)
    defn = v.definition
    last = db.scalar(select(BacktestRun).where(BacktestRun.strategy_id == s.id, BacktestRun.status == "completed", BacktestRun.kind == "single").order_by(BacktestRun.created_at.desc()).limit(1))
    out = {"id": str(s.id), "name": s.name, "description": s.description, "status": s.status, "current_version": s.current_version, "instrument_id": str(s.instrument_id),
           "kind": "intraday" if defn else "legacy_daily", "source_type": v.source_type or "legacy", "market": (defn or {}).get("market") or "equity_in",
           "decision_timeframe": (defn or {}).get("decision_timeframe") or "1d", "created_at": s.created_at.isoformat(), "updated_at": s.updated_at.isoformat(),
           "last_backtest": None if last is None else {"id": str(last.id), "at": last.created_at.isoformat(), "version": last.strategy_version,
                                                         "trades": (last.result or {}).get("metrics", {}).get("total_trades"),
                                                         "net_profit": (last.result or {}).get("metrics", {}).get("net_profit"),
                                                         "return_pct": (last.result or {}).get("metrics", {}).get("return_pct")},
           "params": v.params if not defn else None}
    prov = ((defn or {}).get("source") or {}).get("provenance") or {}
    out["origin"] = "starter" if v.source_type == "starter" else ("customized" if prov.get("derived_from") else None)
    out["origin_label"] = STARTER_LABEL if out["origin"] == "starter" else None
    out["starter_key"] = prov.get("starter_key")
    out["derived_from"] = prov.get("derived_from")
    out["indicator_summary"] = _summary(defn)
    out["tested"] = last is not None
    opts = (defn or {}).get("options")
    out["strategy_type"] = "options_buying" if opts else ("intraday" if defn else "legacy_daily")
    if opts:
        ex, u = opts["execution"], opts["underlying"]
        out["options_summary"] = {"market": f"{u['underlying']} options", "type": "Options buying", "directions": [d for d, ok in (("CE", opts["selection"]["allow_ce"]), ("PE", opts["selection"]["allow_pe"])) if ok],
                                  "base_data": "1-minute", "trend_timeframe": u["trend_timeframe"], "entry_timeframe": u["entry_timeframe"], "max_duration_minutes": ex["max_hold_minutes"],
                                  "premium_stop_pct": ex["premium_stop_pct"], "target_r": ex["target_r"], "price_mode": ex["price_mode"]}
        out["data_coverage"] = _options_coverage_brief(db, u["underlying"])
    if full:
        out["versions"] = [{"version": x.version, "created_at": x.created_at.isoformat(), "source_type": x.source_type or "legacy", "definition": x.definition, "params": x.params,
                            "definition_hash": x.definition_hash, "locked": x.locked_at is not None, "has_source": bool(x.source_artifact),
                            "source_filename": (x.source_artifact or {}).get("filename")}
                           for x in db.scalars(select(StrategyVersion).where(StrategyVersion.strategy_id == s.id).order_by(StrategyVersion.version.desc()))]
        out["tunable_parameters"] = tunable_parameters(StrategyDefinition.model_validate(defn)) if defn else []
    return out


@router.get("/strategies")
def list_strategies(active_session: UserSession = Depends(require_verified_session), db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    """Complete inventory: legacy daily strategies (all statuses) and intraday strategies."""
    rows = db.scalars(select(Strategy).where(Strategy.user_id == active_session.user_id).order_by(Strategy.created_at.desc()))
    return [_strategy_out(db, s) for s in rows]


@router.get("/strategies/{strategy_id}")
def get_strategy(strategy_id: UUID, active_session: UserSession = Depends(require_verified_session), db: Session = Depends(get_db)) -> dict[str, Any]:
    return _strategy_out(db, _owned(db, active_session.user_id, strategy_id), full=True)


@router.post("/validate")
def validate(body: DefinitionBody, _: UserSession = Depends(require_verified_session)) -> dict[str, Any]:
    try:
        defn = StrategyDefinition.model_validate(body.definition)
    except ValidationError as e:
        return {"valid": False, "issues": [{"level": "error", "where": ".".join(map(str, x["loc"])), "message": x["msg"]} for x in e.errors()][:30], "requirements": None}
    issues, req = validate_definition(defn)
    return {"valid": req is not None, "issues": issues, "definition_hash": definition_hash(defn),
            "requirements": None if req is None else {"timeframes": req.timeframes, "indicators": req.indicators, "warmup_base_minutes": req.warmup_base_minutes, "condition_nodes": req.nodes}}


@router.post("/strategies", status_code=201)
def create_strategy(body: DefinitionBody, active_session: UserSession = Depends(require_verified_session), _: UserSession = Depends(require_csrf),
                    db: Session = Depends(get_db)) -> dict[str, Any]:
    defn = _parse_definition(body.definition)
    _check(defn)
    anchor = _anchor_instrument(db, defn)
    now = _now()
    s = Strategy(user_id=active_session.user_id, instrument_id=anchor.id, name=defn.name, description=defn.description, status="suspended", current_version=1, created_at=now, updated_at=now)
    db.add(s)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "A strategy with this name already exists") from None
    db.add(_new_version_row(defn, s.id, 1, body.source_text, now))
    db.commit()
    db.refresh(s)
    return _strategy_out(db, s, full=True)


@router.post("/engines/scalp_hold", status_code=201)
def install_scalp_hold(active_session: UserSession = Depends(require_verified_session), _: UserSession = Depends(require_csrf), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Add the scalp_hold engine strategy to the owner's library (idempotent)."""
    from app.services.engine_strategies import ensure_scalp_hold

    try:
        s = ensure_scalp_hold(db, active_session.user_id)
    except ValueError as error:
        raise HTTPException(409, str(error)) from None
    return _strategy_out(db, s, full=True)


@router.get("/engines/scalp_hold/geometry")
def scalp_hold_geometry(instrument_id: UUID, start: date, end: date, bar_minutes: int = Query(default=5, ge=1, le=60), H_minutes: float = Query(default=30, gt=0),
                        equity: float = Query(default=10000, gt=0), leverage: float = Query(default=1, ge=1), active_session: UserSession = Depends(require_verified_session),
                        db: Session = Depends(get_db)) -> dict[str, Any]:
    """Read-only inputs for the engine-parameter preview: round-trip cost at the run's reference notional, the typical bar range and median sigma_H of the
    selected instrument over the window. Nothing is stored; seasonality is not fitted here (factor 1), so sigma_H is the un-seasoned estimate."""
    import numpy as np
    import pandas as pd

    from scalp_hold.app_adapter import DEFAULT_PARAMS, prepare_frame, venue_for
    from scalp_hold.features import FeatureCfg, Seasonal, atr, sigma_bar, sigma_h
    from scalp_hold.venues import round_trip_pct

    inst = db.get(Instrument, instrument_id)
    if inst is None:
        raise HTTPException(404, "Instrument not found")
    raw = runner.load_bars(db, inst.id, datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc), datetime.combine(end, datetime.max.time(), tzinfo=timezone.utc))
    out: dict[str, Any] = {"instrument_id": str(instrument_id), "symbol": inst.symbol, "bar_minutes": bar_minutes, "H_minutes": H_minutes, "seasonal": "not fitted (factor 1.0)"}
    if raw.empty:
        return {**out, "available": False, "reason": "no stored 1-minute bars in this window"}
    df = raw.copy()
    df.index = pd.DatetimeIndex(df.pop("time"), name="ts_utc")
    market = "crypto" if inst.asset_class == "crypto" else "equity_in"
    venue = venue_for(market, inst.asset_class, DEFAULT_PARAMS)
    frame, info = prepare_frame(df[["open", "high", "low", "close", "volume"]].astype(float), venue, bar_minutes, 100.0)
    if frame is None:
        return {**out, "available": False, "reason": info.get("refused", "not enough data")}
    ref_notional = equity * min(leverage, 2.0)
    cost_rt = round_trip_pct(venue, inst.symbol, float(np.nanmedian(frame["close"])), ref_notional)
    bar_pct = (atr(frame, FeatureCfg().atr_n) / frame["close"]).dropna()
    sh = sigma_h(sigma_bar(frame, FeatureCfg()), frame.index, venue, H_minutes, Seasonal()).dropna()
    return {**out, "available": True, "venue": venue.id, "bars_used": int(len(frame)), "reference_notional": ref_notional, "cost_rt_pct": cost_rt * 100,
            "typical_bar_pct": float(bar_pct.median() * 100) if len(bar_pct) else None, "bar_pct_p90": float(bar_pct.quantile(0.9) * 100) if len(bar_pct) else None,
            "sigma_H_median_pct": float(sh.median() * 100) if len(sh) else None, "sigma_H_p90_pct": float(sh.quantile(0.9) * 100) if len(sh) else None,
            "tick_size": str(venue.tick_size), "disclaimer": DISCLAIMER}


@router.post("/strategies/{strategy_id}/versions", status_code=201)
def create_version(strategy_id: UUID, body: DefinitionBody, active_session: UserSession = Depends(require_verified_session), _: UserSession = Depends(require_csrf),
                   db: Session = Depends(get_db)) -> dict[str, Any]:
    s = _owned(db, active_session.user_id, strategy_id)
    if s.status == "retired":
        raise HTTPException(409, "Retired strategies cannot be versioned; duplicate it instead")
    cur = _version(db, s)
    if cur.definition is None:
        raise HTTPException(409, "Legacy daily strategies are versioned from the legacy panel; use 'Convert to intraday draft' to create an intraday copy")
    defn = _parse_definition(body.definition)
    _check(defn)
    anchor = _anchor_instrument(db, defn)
    now = _now()
    nxt = s.current_version + 1
    db.add(_new_version_row(defn, s.id, nxt, body.source_text if body.source_text is not None else (cur.source_artifact or {}).get("text"), now))
    s.current_version, s.updated_at, s.instrument_id, s.description = nxt, now, anchor.id, defn.description
    db.commit()
    db.refresh(s)
    return _strategy_out(db, s, full=True)


@router.post("/strategies/{strategy_id}/duplicate", status_code=201)
def duplicate(strategy_id: UUID, body: DuplicateBody, active_session: UserSession = Depends(require_verified_session), _: UserSession = Depends(require_csrf),
              db: Session = Depends(get_db)) -> dict[str, Any]:
    src = _owned(db, active_session.user_id, strategy_id)
    v = _version(db, src)
    now = _now()
    s = Strategy(user_id=active_session.user_id, instrument_id=src.instrument_id, name=body.name, description=src.description, status="suspended", current_version=1, created_at=now, updated_at=now)
    db.add(s)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "A strategy with this name already exists") from None
    if v.definition is None:  # legacy copy: same stored params, new identity
        db.add(StrategyVersion(strategy_id=s.id, version=1, params=dict(v.params), created_at=now))
    else:
        d = dict(v.definition)
        d["name"] = body.name
        if v.source_type == "starter":  # an editable independent copy that remembers where it came from
            prov = dict(d["source"].get("provenance") or {})
            d["source"] = {"type": "visual", "artifact": None, "provenance": {"derived_from": {"strategy_id": str(src.id), "version": v.version, "starter_key": prov.get("starter_key"), "name": src.name}}}
        db.add(_new_version_row(StrategyDefinition.model_validate(d), s.id, 1, (v.source_artifact or {}).get("text"), now))
    db.commit()
    db.refresh(s)
    return _strategy_out(db, s, full=True)


@router.get("/strategies/{strategy_id}/convert-draft")
def convert_draft(strategy_id: UUID, decision_timeframe: str = Query(default="5m"), active_session: UserSession = Depends(require_verified_session),
                  db: Session = Depends(get_db)) -> dict[str, Any]:
    """Unsaved intraday draft equivalent to a legacy daily strategy's rule (MA crossover [+ RSI filter]). Legacy rows are never modified."""
    s = _owned(db, active_session.user_id, strategy_id)
    v = _version(db, s)
    if v.definition is not None:
        raise HTTPException(409, "Already an intraday strategy")
    p = v.params
    if decision_timeframe not in TIMEFRAME_MINUTES:
        raise HTTPException(422, "unsupported timeframe")
    ma = str(p.get("ma_type", "EMA")).lower()
    ma = ma if ma in ti.REGISTRY else "ema"
    inds = [{"alias": "fast", "indicator": ma, "params": {"length": int(p["fast_period"])}, "timeframe": decision_timeframe},
            {"alias": "slow", "indicator": ma, "params": {"length": int(p["slow_period"])}, "timeframe": decision_timeframe}]
    cross = {"type": "compare", "left": {"kind": "indicator", "ref": "fast"}, "op": "crosses_above", "right": {"kind": "indicator", "ref": "slow"}}
    entry: dict[str, Any] = cross
    if p.get("strategy_id") == "sma_rsi" and p.get("rsi_max") is not None:
        inds.append({"alias": "rsi", "indicator": "rsi", "params": {"length": 14}, "timeframe": decision_timeframe})
        entry = {"type": "group", "op": "and", "children": [cross, {"type": "compare", "left": {"kind": "indicator", "ref": "rsi"}, "op": "<=", "right": {"kind": "const", "value": float(p["rsi_max"])}}]}
    draft = {"schema_version": SCHEMA_VERSION, "name": f"{s.name} (intraday)"[:120], "description": f"Intraday copy of legacy strategy {s.name!r}",
             "market": "equity_in", "universe": [str(s.instrument_id)], "decision_timeframe": decision_timeframe, "indicators": inds, "long_entry": entry,
             "long_exit": {"type": "compare", "left": {"kind": "indicator", "ref": "fast"}, "op": "crosses_below", "right": {"kind": "indicator", "ref": "slow"}},
             "source": {"type": "legacy", "provenance": {"converted_from": str(s.id), "legacy_version": v.version, "legacy_params": p,
                                                          "note": "Legacy rule ran on completed DAILY bars; this draft applies the same logic to intraday candles and is not equivalent."}}}
    return {"definition": draft, "note": draft["source"]["provenance"]["note"]}


# ───────────────────────── imports ─────────────────────────
@router.post("/import/pine")
def import_pine_script(body: ImportBody, _: UserSession = Depends(require_verified_session), __: UserSession = Depends(require_csrf)) -> dict[str, Any]:
    if not body.filename.lower().endswith((".pine", ".txt")):
        raise HTTPException(422, "Pine files must be .pine or .txt")
    if body.decision_timeframe not in TIMEFRAME_MINUTES:
        raise HTTPException(422, "unsupported timeframe")
    return import_pine(body.text, body.filename, body.market, body.decision_timeframe)


@router.post("/import/python")
def import_python(body: ImportBody, _: UserSession = Depends(require_verified_session), __: UserSession = Depends(require_csrf)) -> dict[str, Any]:
    if not body.filename.lower().endswith(".py"):
        raise HTTPException(422, "Python strategies must be .py files")
    errors = ipy.screen_source(body.text)
    report: dict[str, Any] = {"filename": body.filename, "sha256": hashlib.sha256(body.text.encode()).hexdigest(), "screen_errors": errors, "can_import": False, "definition": None,
                              "allowed_imports": list(ipy.ALLOWED_IMPORTS), "isolation": "separate interpreter, AST screen, scrubbed env, rlimits, wall-clock kill (not container-grade)"}
    if errors:
        return report
    try:
        defn, meta_ = ipy.build_definition(body.text, body.filename, body.market)
        issues, req = validate_definition(defn)
        report.update(meta=meta_, issues=issues, can_import=req is not None, definition=defn.model_dump(mode="json") if req is not None else None)
    except ipy.SandboxError as e:
        report["screen_errors"] = [{"line": e.line, "message": e.message}]
    except (ValidationError, ValueError) as e:
        report["screen_errors"] = [{"line": None, "message": f"strategy declaration is invalid: {str(e)[:300]}"}]
    return report


# ───────────────────────── backtests ─────────────────────────
def _run_in_background(bind: Any, run_id: UUID, instrument_ids: list[UUID]) -> None:
    with Session(bind) as db:
        run = db.get(BacktestRun, run_id)
        if run is None:
            return
        run.status, run.progress = "running", 10
        db.commit()
        try:
            strategy = db.get(Strategy, run.strategy_id)
            version = _version(db, strategy, run.strategy_version)
            defn = StrategyDefinition.model_validate(run.definition_snapshot)
            cfg = BacktestConfig.model_validate({k: v for k, v in run.config.items() if k != "_options"})
            insts = runner.resolve_instruments(db, defn, [str(i) for i in instrument_ids])
            options = (run.config or {}).get("_options", {})
            created = run.created_at if run.created_at.tzinfo else run.created_at.replace(tzinfo=timezone.utc)
            result = runner.execute(db, defn, (version.source_artifact or {}).get("text"), run.kind, insts, cfg, options, created)
            run.result, run.status, run.progress = result, "completed", 100
            run.provenance = result.get("dataset")
        except Exception as e:  # noqa: BLE001 - recorded on the run, shown to the user
            run.status, run.error = "failed", str(e)[:1000]
        run.finished_at = _now()
        db.commit()


@router.post("/backtests", status_code=202)
def start_backtest(body: BacktestBody, active_session: UserSession = Depends(require_verified_session),
                   _: UserSession = Depends(require_csrf), db: Session = Depends(get_db)) -> dict[str, Any]:
    s = _owned(db, active_session.user_id, body.strategy_id)
    v = _version(db, s, body.version)
    if v.definition is None:
        raise HTTPException(409, "Legacy daily strategies are backtested from the legacy panel; convert to an intraday draft to use 1-minute backtests")
    try:
        cfg = BacktestConfig.model_validate(body.config)
    except ValidationError as e:
        raise HTTPException(422, detail={"code": "invalid_config", "errors": [{"where": ".".join(map(str, x["loc"])), "message": x["msg"]} for x in e.errors()]}) from None
    defn = StrategyDefinition.model_validate(v.definition)
    try:
        insts = runner.resolve_instruments(db, defn, [str(i) for i in body.instruments])
        runner.window(cfg, _now())
        costs.schedule_for_market(defn.market, cfg.cost_profile)
    except (runner.RunnerError, ValueError) as e:
        raise HTTPException(422, detail={"code": "invalid_backtest", "message": str(e)}) from None
    now = _now()
    start, end = runner.window(cfg, now)
    cfg = cfg.model_copy(update={"start": start, "end": end})  # resolve the window now so the stored run is reproducible
    if defn.options is not None:
        cov, blockers = runner.options_blockers(db, defn, now)
        if blockers:
            raise HTTPException(422, detail={"code": "options_data_unavailable", "message": "Options backtesting is unavailable: " + " | ".join(blockers), "blockers": blockers, "coverage_mode": cov["backtest_mode"]})
    if v.locked_at is None:
        v.locked_at = now  # the version is now referenced by a backtest; its definition can never change (edits create a new version)
    cfg_dump = cfg.model_dump(mode="json")
    cfg_dump["_options"] = body.options
    run = BacktestRun(user_id=active_session.user_id, strategy_id=s.id, strategy_version=v.version, kind=body.kind, status="queued", progress=0, config=cfg_dump,
                      definition_snapshot=v.definition, definition_hash=v.definition_hash or definition_hash(v.definition), engine_version=ENGINE_VERSION,
                      indicator_version=ti.INDICATOR_LIBRARY_VERSION, created_at=now)
    db.add(run)
    db.commit()
    db.refresh(run)
    job, _ = job_queue.enqueue(db, user_id=active_session.user_id, job_type="backtest", max_attempts=3,
                               payload={"run_id": str(run.id), "instrument_ids": [str(i.id) for i in insts]}, idempotency_key=f"backtest:{run.id}")
    return {**_summary_of(run), "job_id": str(job.id)}


@router.get("/backtests")
def list_backtests(strategy_id: UUID | None = None, limit: int = Query(default=30, ge=1, le=100), active_session: UserSession = Depends(require_verified_session),
                   db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    q = select(BacktestRun).where(BacktestRun.user_id == active_session.user_id)
    if strategy_id:
        q = q.where(BacktestRun.strategy_id == strategy_id)
    return [_summary_of(r) for r in db.scalars(q.order_by(BacktestRun.created_at.desc()).limit(limit))]


@router.get("/backtests/{run_id}")
def get_backtest(run_id: UUID, active_session: UserSession = Depends(require_verified_session), db: Session = Depends(get_db)) -> dict[str, Any]:
    run = db.scalar(select(BacktestRun).where(BacktestRun.id == run_id, BacktestRun.user_id == active_session.user_id))
    if run is None:
        raise HTTPException(404, "Backtest not found")
    return _summary_of(run, with_result=True)


# ───────────────────────── live signals ─────────────────────────
def _signal_out(db: Session, x: IntradaySignal) -> dict[str, Any]:
    inst = db.get(Instrument, x.instrument_id)
    strat = db.get(Strategy, x.strategy_id)
    return {"id": str(x.id), "strategy_id": str(x.strategy_id), "strategy_name": strat.name if strat else None, "strategy_version": x.strategy_version,
            "instrument_id": str(x.instrument_id), "symbol": inst.symbol if inst else None, "market": x.market, "direction": x.direction, "status": x.status,
            "suppression_reason": x.suppression_reason, "timeframe": x.timeframe, "signal_time": x.signal_time.isoformat(), "reference_price": str(x.reference_price),
            "currency": inst.currency if inst else None, "indicators": x.indicators, "conditions_met": x.conditions_met, "conditions_unmet": x.conditions_unmet,
            "rationale": x.rationale, "data_freshness": runner.freshness_view(x.data_freshness, _now()), "expires_at": x.expires_at.isoformat(), "created_at": x.created_at.isoformat(), "disclaimer": DISCLAIMER}


@router.post("/signals/scan", status_code=202)
def scan(body: ScanBody, active_session: UserSession = Depends(require_verified_session), _: UserSession = Depends(require_csrf), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Queue a scan of the owner's active strategies (one job per strategy, grouped by ``scan_run_id``). Poll ``GET /scan-runs/{scan_run_id}``.

    A strategy whose previous scan is still queued or running is skipped, never overlapped.
    """
    q = select(Strategy).where(Strategy.user_id == active_session.user_id, Strategy.status == "active")
    if body.strategy_ids:
        q = q.where(Strategy.id.in_(body.strategy_ids))
    scan_run_id = uuid4()
    queued: list[str] = []
    in_flight: list[str] = []
    for s in db.scalars(q):
        v = _version(db, s)
        if v.definition is None:
            continue
        key = f"scan:{s.id}"
        if db.scalar(select(Job.id).where(Job.user_id == active_session.user_id, Job.concurrency_key == key, Job.status.in_(("queued", "running"))).limit(1)) is not None:
            in_flight.append(s.name)
            continue
        job, _ = job_queue.enqueue(db, user_id=active_session.user_id, job_type="scan", concurrency_key=key, max_attempts=scan_scheduler.SCAN_MAX_ATTEMPTS,
                                   payload={"strategy_id": str(s.id), "scan_run_id": str(scan_run_id), "trigger": "manual"})
        queued.append(str(job.id))
    return {"scan_run_id": str(scan_run_id), "job_ids": queued, "strategies_queued": len(queued), "skipped_in_flight": in_flight, "disclaimer": DISCLAIMER}


def _scan_run_view(db: Session, user_id: UUID, scan_run_id: UUID) -> dict[str, Any] | None:
    rows = list(db.scalars(select(ScanRun).where(ScanRun.user_id == user_id, ScanRun.scan_run_id == scan_run_id).order_by(ScanRun.started_at)))
    jobs = list(db.scalars(select(Job).where(Job.user_id == user_id, Job.job_type == "scan", Job.payload["scan_run_id"].as_string() == str(scan_run_id))))
    if not rows and not jobs:
        return None
    by_reason: dict[str, int] = {}
    for r in rows:
        for code, n in (r.by_reason or {}).items():
            by_reason[code] = by_reason.get(code, 0) + n
    pending = [j for j in jobs if j.status in ("queued", "running")]
    failed = [j for j in jobs if j.status == "failed"]
    errors = [e for r in rows for e in (r.errors or [])]
    return {
        "scan_run_id": str(scan_run_id), "status": "running" if pending else "failed" if failed and not rows else "completed",
        "trigger": rows[0].trigger if rows else (jobs[0].payload.get("trigger") if jobs else None),
        "started_at": min((r.started_at for r in rows), default=min((j.created_at for j in jobs), default=None)).isoformat() if (rows or jobs) else None,
        "finished_at": None if pending else max((r.finished_at for r in rows if r.finished_at), default=None).isoformat() if any(r.finished_at for r in rows) else None,
        "strategies": len(jobs) or len(rows), "pairs_scanned": sum(r.pairs_scanned for r in rows),
        "instruments_scanned": len({i for r in rows for i in (r.instrument_ids or [])}), "new_signals": sum(r.new_signals for r in rows),
        "errors": errors + [{"strategy": str(j.payload.get("strategy_id")), "error": j.error or "job failed"} for j in failed],
        "rejections": {"scan_run_id": str(scan_run_id), "total": sum(by_reason.values()), "by_reason": dict(sorted(by_reason.items(), key=lambda kv: (-kv[1], kv[0])))},
        "jobs": [{"id": str(j.id), "status": j.status, "strategy_id": j.payload.get("strategy_id"), "error": j.error} for j in jobs],
        "disclaimer": DISCLAIMER,
    }


@router.get("/scan-runs/{scan_run_id}")
def get_scan_run(scan_run_id: UUID, active_session: UserSession = Depends(require_verified_session), db: Session = Depends(get_db)) -> dict[str, Any]:
    view = _scan_run_view(db, active_session.user_id, scan_run_id)
    if view is None:
        raise HTTPException(404, "Scan run not found")
    return view


@router.get("/scan-runs")
def list_scan_runs(limit: int = Query(default=30, ge=1, le=100), active_session: UserSession = Depends(require_verified_session), db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    """Scan history, newest first: when each ran, what it covered, signals and suppressions."""
    ids: list[UUID] = []
    for (rid,) in db.execute(select(ScanRun.scan_run_id, func.min(ScanRun.started_at).label("t")).where(ScanRun.user_id == active_session.user_id)
                             .group_by(ScanRun.scan_run_id).order_by(func.min(ScanRun.started_at).desc()).limit(limit)):
        ids.append(rid)
    return [v for rid in ids if (v := _scan_run_view(db, active_session.user_id, rid)) is not None]


_STATE_FILTER = "^(forming|ready|active|partially_realised|target|stopped|invalidated|expired|cancelled|new|suppressed)$"


@router.get("/signals")
def list_signals(status: str | None = Query(default=None, pattern=_STATE_FILTER), strategy_id: UUID | None = None,
                 limit: int = Query(default=100, ge=1, le=300), active_session: UserSession = Depends(require_verified_session), db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    now = _now()
    due = db.scalars(select(IntradaySignal).where(IntradaySignal.user_id == active_session.user_id, IntradaySignal.status.in_(lifecycle.OPEN_STATES),
                                                  IntradaySignal.expires_at <= now))
    for row in due:  # expiry is time-derived; persist it as an append-only transition
        lifecycle.transition_signal(db, row, "expired", reason_code="time_expired", observed_data_time=row.expires_at, event_time=now, source="lazy_expiry")
    db.commit()
    if status == "suppressed":  # legacy filter value: suppressed candidates are scan rejections, not signals
        return []
    status = "ready" if status == "new" else status  # legacy alias
    q = select(IntradaySignal).where(IntradaySignal.user_id == active_session.user_id)
    if status:
        q = q.where(IntradaySignal.status == status)
    if strategy_id:
        q = q.where(IntradaySignal.strategy_id == strategy_id)
    rows = list(db.scalars(q.order_by(IntradaySignal.created_at.desc()).limit(limit)))
    return [_signal_out(db, r) for r in rows]


def _owned_signal(db: Session, signal_id: UUID, user_id: UUID) -> IntradaySignal:
    row = db.scalar(select(IntradaySignal).where(IntradaySignal.id == signal_id, IntradaySignal.user_id == user_id))
    if row is None:
        raise HTTPException(404, "Signal not found")
    return row


@router.get("/signals/{signal_id}/events")
def signal_events(signal_id: UUID, active_session: UserSession = Depends(require_verified_session), db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    _owned_signal(db, signal_id, active_session.user_id)
    rows = db.scalars(select(IntradaySignalEvent).where(IntradaySignalEvent.signal_id == signal_id).order_by(IntradaySignalEvent.event_time, IntradaySignalEvent.event_id))
    return [{"event_id": str(e.event_id), "from_state": e.from_state, "to_state": e.to_state, "event_time": e.event_time.isoformat(),
             "observed_data_time": e.observed_data_time.isoformat() if e.observed_data_time else None, "reason_code": e.reason_code, "reason_detail": e.reason_detail,
             "strategy_version_id": str(e.strategy_version_id), "source": e.source, "payload": e.payload, "backfilled": e.source == "migration_backfill"} for e in rows]


@router.post("/signals/{signal_id}/cancel")
def cancel_signal(signal_id: UUID, active_session: UserSession = Depends(require_verified_session), _: UserSession = Depends(require_csrf),
                  db: Session = Depends(get_db)) -> dict[str, Any]:
    row = _owned_signal(db, signal_id, active_session.user_id)
    seen = lifecycle.last_observed_data_time(db, row.id) or row.signal_time  # an owner action is not caused by new market data: carry the last data time on record
    try:
        lifecycle.transition_signal(db, row, "cancelled", reason_code="owner_cancel", observed_data_time=seen, event_time=_now(), source="manual",
                                    payload={"actor": "owner", "observed_data_time_is": "last data time on record, not new data"})
    except lifecycle.InvalidTransition as exc:
        raise HTTPException(409, str(exc)) from None
    db.commit()
    return _signal_out(db, row)


def _rejection_summary(db: Session, user_id: UUID, scan_run_id: UUID) -> dict[str, Any]:
    rows = db.execute(select(ScanRejection.reason_code, ScanRejection.strategy_id, ScanRejection.instrument_id)
                      .where(ScanRejection.user_id == user_id, ScanRejection.scan_run_id == scan_run_id)).all()
    by_reason: dict[str, int] = {}
    for code, _, _ in rows:
        by_reason[code] = by_reason.get(code, 0) + 1
    return {"scan_run_id": str(scan_run_id), "total": len(rows), "by_reason": dict(sorted(by_reason.items(), key=lambda kv: (-kv[1], kv[0]))),
            "strategies_with_rejections": len({r[1] for r in rows}), "instruments_with_rejections": len({r[2] for r in rows})}


@router.get("/signals/rejections/summary")
def rejections_summary(scan_run_id: UUID | None = None, active_session: UserSession = Depends(require_verified_session), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Counts of suppressed scan candidates by reason for one scan run (default: the user's most recent run)."""
    if scan_run_id is None:
        scan_run_id = db.scalar(select(ScanRejection.scan_run_id).where(ScanRejection.user_id == active_session.user_id, ScanRejection.scan_run_id.is_not(None))
                                .order_by(ScanRejection.recorded_at.desc()).limit(1))
    if scan_run_id is None:
        return {"scan_run_id": None, "total": 0, "by_reason": {}, "strategies_with_rejections": 0, "instruments_with_rejections": 0, "disclaimer": DISCLAIMER}
    return {**_rejection_summary(db, active_session.user_id, scan_run_id), "disclaimer": DISCLAIMER}


@router.get("/signals/rejections")
def list_rejections(scan_run_id: UUID | None = None, limit: int = Query(default=100, ge=1, le=300), active_session: UserSession = Depends(require_verified_session),
                    db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    q = select(ScanRejection, StrategyVersion.version).outerjoin(StrategyVersion, StrategyVersion.id == ScanRejection.strategy_version_id).where(ScanRejection.user_id == active_session.user_id)
    if scan_run_id:
        q = q.where(ScanRejection.scan_run_id == scan_run_id)
    return [{"id": str(r.id), "scan_run_id": str(r.scan_run_id) if r.scan_run_id else None, "strategy_id": str(r.strategy_id), "strategy_version": version,
             "instrument_id": str(r.instrument_id), "direction": r.direction, "reason_code": r.reason_code, "reason_detail": r.reason_detail, "recorded_at": r.recorded_at.isoformat(),
             "observed_data_time": r.observed_data_time.isoformat() if r.observed_data_time else None, "payload": r.payload}
            for r, version in db.execute(q.order_by(ScanRejection.recorded_at.desc()).limit(limit))]


# ───────────────────────── jobs and scanner schedule ─────────────────────────
class ScannerSettingsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scans_enabled: bool


class ScheduleBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool
    cadence_minutes: int | None = Field(default=None, ge=1, le=240)


def _job_out(j: Job) -> dict[str, Any]:
    return {"id": str(j.id), "job_type": j.job_type, "status": j.status, "attempts": j.attempts, "max_attempts": j.max_attempts, "progress": j.progress, "result": j.result,
            "error": j.error, "created_at": j.created_at.isoformat(), "started_at": j.started_at.isoformat() if j.started_at else None,
            "finished_at": j.finished_at.isoformat() if j.finished_at else None, "next_attempt_at": j.next_attempt_at.isoformat(), "locked_by": j.locked_by,
            "payload": {k: v for k, v in (j.payload or {}).items() if k in ("run_id", "strategy_id", "scan_run_id", "trigger", "candle_close")}}


def _owned_job(db: Session, job_id: UUID, user_id: UUID) -> Job:
    j = db.scalar(select(Job).where(Job.id == job_id, Job.user_id == user_id))
    if j is None:
        raise HTTPException(404, "Job not found")
    return j


@router.get("/jobs")
def list_jobs(status: str | None = Query(default=None, pattern="^(queued|running|succeeded|failed|cancelled)$"), limit: int = Query(default=50, ge=1, le=200),
              active_session: UserSession = Depends(require_verified_session), db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    q = select(Job).where(Job.user_id == active_session.user_id)
    if status:
        q = q.where(Job.status == status)
    return [_job_out(j) for j in db.scalars(q.order_by(Job.created_at.desc()).limit(limit))]


@router.get("/jobs/{job_id}")
def get_job(job_id: UUID, active_session: UserSession = Depends(require_verified_session), db: Session = Depends(get_db)) -> dict[str, Any]:
    return _job_out(_owned_job(db, job_id, active_session.user_id))


@router.post("/jobs/{job_id}/retry")
def retry_job(job_id: UUID, active_session: UserSession = Depends(require_verified_session), _: UserSession = Depends(require_csrf), db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        return _job_out(job_queue.retry(db, _owned_job(db, job_id, active_session.user_id)))
    except ValueError as error:
        raise HTTPException(409, str(error)) from None
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Another run for the same strategy is in progress") from None


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: UUID, active_session: UserSession = Depends(require_verified_session), _: UserSession = Depends(require_csrf), db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        return _job_out(job_queue.cancel(db, _owned_job(db, job_id, active_session.user_id)))
    except ValueError as error:
        raise HTTPException(409, str(error)) from None


def _schedule_view(db: Session, user_id: UUID) -> dict[str, Any]:
    settings_row = db.get(ScannerSettings, user_id)
    scheds = {s.strategy_id: s for s in db.scalars(select(ScannerSchedule).where(ScannerSchedule.user_id == user_id))}
    out = []
    for st in db.scalars(select(Strategy).where(Strategy.user_id == user_id, Strategy.status == "active").order_by(Strategy.name)):
        v = _version(db, st)
        if v.definition is None:
            continue
        defn = StrategyDefinition.model_validate(v.definition)
        sc = scheds.get(st.id)
        base = scan_scheduler.default_cadence(defn)
        out.append({"strategy_id": str(st.id), "name": st.name, "market": defn.market, "decision_timeframe": defn.decision_timeframe, "min_cadence_minutes": base,
                    "enabled": bool(sc and sc.enabled), "cadence_minutes": sc.cadence_minutes if sc else base,
                    "next_run_at": sc.next_run_at.isoformat() if sc and sc.next_run_at and sc.enabled else None,
                    "last_enqueued_at": sc.last_enqueued_at.isoformat() if sc and sc.last_enqueued_at else None, "last_skip_reason": sc.last_skip_reason if sc else None})
    return {"scans_enabled": bool(settings_row and settings_row.scans_enabled), "strategies": out, "request_estimate": scan_scheduler.request_estimate(db, user_id),
            "note": "Scans run on the server at decision-candle close on NSE trading days (09:15-15:30 IST). Turning the master switch off stops all scheduled scans.", "disclaimer": DISCLAIMER}


@router.get("/scanner/schedule")
def get_scanner_schedule(active_session: UserSession = Depends(require_verified_session), db: Session = Depends(get_db)) -> dict[str, Any]:
    return _schedule_view(db, active_session.user_id)


@router.put("/scanner/settings")
def put_scanner_settings(body: ScannerSettingsBody, active_session: UserSession = Depends(require_verified_session), _: UserSession = Depends(require_csrf),
                         db: Session = Depends(get_db)) -> dict[str, Any]:
    row = db.get(ScannerSettings, active_session.user_id)
    if row is None:
        row = ScannerSettings(user_id=active_session.user_id, scans_enabled=body.scans_enabled, updated_at=_now())
        db.add(row)
    row.scans_enabled, row.updated_at = body.scans_enabled, _now()
    db.commit()
    return _schedule_view(db, active_session.user_id)


@router.put("/scanner/schedule/{strategy_id}")
def put_scanner_schedule(strategy_id: UUID, body: ScheduleBody, active_session: UserSession = Depends(require_verified_session), _: UserSession = Depends(require_csrf),
                         db: Session = Depends(get_db)) -> dict[str, Any]:
    st = _owned(db, active_session.user_id, strategy_id)
    v = _version(db, st)
    if v.definition is None:
        raise HTTPException(409, "Only intraday strategies can be scanned on a schedule")
    defn = StrategyDefinition.model_validate(v.definition)
    cadence = body.cadence_minutes or scan_scheduler.default_cadence(defn)
    if not scan_scheduler.valid_cadence(defn, cadence):
        raise HTTPException(422, f"Cadence must be a multiple of the {defn.decision_timeframe} decision timeframe ({scan_scheduler.default_cadence(defn)} min)")
    sc = db.scalar(select(ScannerSchedule).where(ScannerSchedule.user_id == active_session.user_id, ScannerSchedule.strategy_id == strategy_id))
    if sc is None:
        sc = ScannerSchedule(user_id=active_session.user_id, strategy_id=strategy_id, enabled=False, cadence_minutes=cadence, updated_at=_now())
        db.add(sc)
    changed = sc.cadence_minutes != cadence or sc.enabled != body.enabled
    sc.enabled, sc.cadence_minutes, sc.updated_at = body.enabled, cadence, _now()
    if changed or sc.next_run_at is None:
        sc.next_run_at = scan_scheduler.next_scan_time(_now(), cadence, defn.market) if body.enabled else None
        sc.last_skip_reason = None
    db.commit()
    return _schedule_view(db, active_session.user_id)


# ───────────────────────── built-in starter strategies ─────────────────────────
def _readiness(db: Session) -> dict[str, Any]:
    stats: dict[str, int] = {"equity_in": 0, "crypto": 0}
    have = {r[0] for r in db.execute(select(MarketBar.instrument_id).where(MarketBar.interval == "1m", MarketBar.is_synthetic.is_(False)).distinct())}
    for inst in db.scalars(select(Instrument).where(Instrument.id.in_(have))) if have else []:
        try:
            stats[profile_for(inst).key] += 1
        except UnsupportedMarket:
            continue
    return {k: {"instruments_with_1m_data": n, "backtest_ready": n > 0} for k, n in stats.items()}


@router.get("/starters")
def starters(active_session: UserSession = Depends(require_verified_session), db: Session = Depends(get_db)) -> dict[str, Any]:
    owned = {}
    for s in db.scalars(select(Strategy).where(Strategy.user_id == active_session.user_id)):
        key = (((_version(db, s).definition or {}).get("source") or {}).get("provenance") or {}).get("starter_key")
        if key and _version(db, s).source_type == "starter":
            owned[key] = str(s.id)
    return {"label": STARTER_LABEL, "catalog": [{"key": d["source"]["provenance"]["starter_key"], "name": d["name"], "description": d["description"], "indicators": _summary(d),
                                                 "installed_strategy_id": owned.get(d["source"]["provenance"]["starter_key"])} for d in STARTERS],
            "readiness": _readiness(db), "disclaimer": DISCLAIMER}


@router.post("/starters/install")
def install_starters(active_session: UserSession = Depends(require_verified_session), _: UserSession = Depends(require_csrf), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Idempotent: creates any missing starter for this user (suspended, empty universe). Never touches existing strategies."""
    uid = active_session.user_id
    existing = list(db.scalars(select(Strategy).where(Strategy.user_id == uid)))
    names = {s.name for s in existing}
    keys = set()
    for s in existing:
        v = _version(db, s)
        if v.source_type == "starter":
            keys.add(((v.definition or {}).get("source") or {}).get("provenance", {}).get("starter_key"))
    missing = [d for d in STARTERS if d["source"]["provenance"]["starter_key"] not in keys]
    if not missing:
        return {"installed": 0, "skipped": [], "total": len(STARTERS)}
    counts = {r[0]: r[1] for r in db.execute(select(MarketBar.instrument_id, func.count()).where(MarketBar.interval == "1m", MarketBar.is_synthetic.is_(False)).group_by(MarketBar.instrument_id))}
    anchor = None
    best = -1
    for inst in db.scalars(select(Instrument).where(Instrument.is_synthetic.is_(False)).limit(5000)):
        try:
            prof = profile_for(inst)
        except UnsupportedMarket:
            continue
        score = counts.get(inst.id, 0) + (1 if prof.key == "equity_in" else 0)
        if score > best:
            anchor, best = inst, score
    if anchor is None:
        return {"installed": 0, "skipped": [{"name": d["name"], "reason": "no eligible instrument exists to anchor the strategy"} for d in missing], "total": len(STARTERS)}
    now, installed, skipped = _now(), 0, []
    for d in missing:
        if d["name"] in names:
            skipped.append({"name": d["name"], "reason": "a strategy with this name already exists and was left untouched"})
            continue
        defn = StrategyDefinition.model_validate(d)
        row = Strategy(user_id=uid, instrument_id=anchor.id, name=defn.name, description=defn.description, status="suspended", current_version=1, created_at=now, updated_at=now)
        db.add(row)
        try:
            db.flush()
        except IntegrityError:  # concurrent install: the other request won
            db.rollback()
            continue
        db.add(_new_version_row(defn, row.id, 1, None, now))
        installed += 1
    db.commit()
    return {"installed": installed, "skipped": skipped, "total": len(STARTERS)}


# ───────────────────────── options research ─────────────────────────
@router.get("/options/coverage")
def options_coverage(underlying: str = Query(default="NIFTY", min_length=1, max_length=32), _: UserSession = Depends(require_verified_session), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Measured (not assumed) coverage of NIFTY spot/futures/option data and whether a backtest can run, and in which mode."""
    return coverage_report(db, _now(), underlying)


@router.post("/options/compile")
def options_compile(body: dict[str, Any], _: UserSession = Depends(require_verified_session), __: UserSession = Depends(require_csrf)) -> dict[str, Any]:
    """Regenerate the underlying-signal indicators and condition trees from the options underlying settings (the Builder stores the result as normal data)."""
    try:
        u = OptionsUnderlying.model_validate(body)
    except ValidationError as e:
        raise HTTPException(422, detail={"code": "invalid_options_settings", "errors": [{"where": ".".join(map(str, x["loc"])), "message": x["msg"]} for x in e.errors()][:20]}) from None
    return build_underlying_rules(u)
