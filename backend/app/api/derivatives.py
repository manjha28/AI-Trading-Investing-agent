"""Dhan derivatives data platform API: capabilities, universe, contracts, coverage, ingestion jobs. Data endpoints are read-only; mutations require CSRF.
No order placement. Tokens are never returned."""
import threading
import re
from datetime import date, datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.auth import require_csrf
from app.api.strategies import require_verified_session
from app.db import engine, get_db
from app.models import (DerivativeContract, DerivativeDataCoverage, DerivativeIngestionJob, DerivativeIngestionTask, DerivativeInstrument, FuturesCandle1m, OptionCandle1m, OptionChainSnapshot, UserSession)
from app.models.strategies import OptionContract, OptionQuote
from app.services import derivatives_ingest as di
from app.services import dhan_derivatives as dd

router = APIRouter(prefix="/derivatives", tags=["derivatives"])
DISCLAIMER = "Market-data infrastructure and research only: nothing here places orders. Coverage reflects what has actually been downloaded."
_THREADS: dict[UUID, threading.Thread] = {}


class JobIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str = Field(pattern="^(futures|option_candles|rolling|chains|master_refresh)$")
    exchange: str | None = Field(default=None, pattern="^(NSE|BSE|MCX)$")
    segment: str | None = Field(default=None, max_length=16)
    instrument_type: str | None = Field(default=None, max_length=8)
    underlying: str | None = Field(default=None, max_length=64)
    category: str | None = Field(default=None, pattern="^(index|stock|commodity|currency)$")
    days: int = Field(default=89, ge=1, le=2000)
    max_offset: int = Field(default=2, ge=0, le=20)
    expiry_codes: list[int] = Field(default_factory=lambda: [1], max_length=3)
    half_width: int = Field(default=5, ge=0, le=60)
    expiries: int = Field(default=1, ge=1, le=6)
    priority: list[str] = Field(default_factory=list, max_length=100)
    limit: int = Field(default=500, ge=1, le=5000)
    dry_run: bool = True
    daily: bool = True


def _plan(db: Session, b: JobIn) -> tuple[list[di.TaskSpec], dict[str, Any]]:
    scope = {k: v for k, v in dict(exchange=b.exchange, segment=b.segment, instrument_type=b.instrument_type, underlying=b.underlying, category=b.category, limit=b.limit).items() if v}
    now = di.utcnow()
    if b.kind == "futures":
        return di.plan_futures(db, scope, lookback_days=b.days, daily=b.daily, now=now), scope
    if b.kind == "option_candles":
        if b.underlying:
            return di.plan_option_candles_near_atm(db, b.underlying, exchange=b.exchange or "NSE", half_width=b.half_width, lookback_days=min(b.days, 89), now=now), scope
        return di.plan_option_candles(db, scope, lookback_days=min(b.days, 89), now=now), scope
    if b.kind == "rolling":
        from datetime import timedelta
        return di.plan_rolling(db, scope, first=(now - timedelta(days=b.days)).date(), last=(now - timedelta(days=1)).date(), max_offset=b.max_offset, expiry_codes=tuple(b.expiry_codes)), scope
    if b.kind == "chains":
        return di.plan_chains(db, priority_underlyings=list(b.priority) or di.strategy_underlyings(db), expiry_count=b.expiries, limit=b.limit), scope
    return [di.TaskSpec(f"master:{now:%Y%m%d%H%M}", "master_refresh", {})], scope


def start_job_thread(job_id: UUID, user_id: UUID) -> None:
    if job_id in _THREADS and _THREADS[job_id].is_alive():
        return

    def target() -> None:
        sf = lambda: Session(engine)  # noqa: E731
        try:
            di.run_job(sf, job_id, di.RenewingClient(sf, user_id), sleep=__import__("time").sleep)
            with sf() as db:
                di.refresh_coverage(db)
        except Exception:  # noqa: BLE001 - the job row records its own failures
            pass

    t = threading.Thread(target=target, name=f"derivatives-job-{job_id}", daemon=True)
    _THREADS[job_id] = t
    t.start()


def _job_out(j: DerivativeIngestionJob, attempts: int = 0) -> dict[str, Any]:
    pct = 0 if not j.tasks_total else round(100 * (j.tasks_done + j.tasks_failed) / j.tasks_total, 1)
    params = {k: v for k, v in (j.params or {}).items() if k != "args"}
    if "request" not in params and isinstance((j.params or {}).get("args"), dict):
        params["request"] = {k: v for k, v in j.params["args"].items() if k not in ("cmd", "kind")}
    return {"id": str(j.id), "kind": j.kind, "status": j.status, "priority": j.priority, "progress_pct": pct, "tasks_total": j.tasks_total, "tasks_done": j.tasks_done, "tasks_failed": j.tasks_failed, "attempts": attempts,
            "rows_written": j.rows_written, "error": j.error, "cancel_requested": j.cancel_requested, "created_at": j.created_at.isoformat(), "started_at": j.started_at.isoformat() if j.started_at else None,
            "finished_at": j.finished_at.isoformat() if j.finished_at else None, "params": params}


@router.get("/capabilities")
def capabilities(_: UserSession = Depends(require_verified_session)) -> dict[str, Any]:
    return {"capabilities": dd.CAPABILITIES, "limits": {"intraday_max_days": dd.INTRADAY_MAX_DAYS, "rolling_max_span_days": dd.ROLLING_MAX_SPAN_DAYS, "rolling_earliest": dd.ROLLING_EARLIEST.isoformat(),
                                                         "chain_min_interval_seconds": dd.CHAIN_MIN_INTERVAL_SECONDS}, "measured_on": "2026-11-05", "disclaimer": DISCLAIMER}


@router.get("/universe")
def universe(_: UserSession = Depends(require_verified_session), db: Session = Depends(get_db)) -> dict[str, Any]:
    return {**di.inventory(db), "disclaimer": DISCLAIMER}


@router.get("/underlyings")
def underlyings(exchange: str | None = None, category: str | None = None, has_options: bool | None = None, has_futures: bool | None = None, q: str = Query(default="", max_length=40),
                limit: int = Query(default=100, ge=1, le=1000), offset: int = Query(default=0, ge=0), _: UserSession = Depends(require_verified_session), db: Session = Depends(get_db)) -> dict[str, Any]:
    stmt = select(DerivativeInstrument)
    for col, val in ((DerivativeInstrument.exchange, exchange), (DerivativeInstrument.category, category), (DerivativeInstrument.has_options, has_options), (DerivativeInstrument.has_futures, has_futures)):
        if val is not None:
            stmt = stmt.where(col == val)
    if q:
        stmt = stmt.where(DerivativeInstrument.underlying_symbol.ilike(f"%{q}%"))
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(stmt.order_by(DerivativeInstrument.exchange, DerivativeInstrument.underlying_symbol).limit(limit).offset(offset))
    return {"total": total, "items": [{"id": str(u.id), "exchange": u.exchange, "segment": u.segment, "category": u.category, "symbol": u.underlying_symbol, "underlying_security_id": u.underlying_security_id,
                                       "cash_security_id": u.cash_security_id, "cash_segment": u.cash_segment, "has_futures": u.has_futures, "has_options": u.has_options, "status": u.status} for u in rows]}


@router.get("/contracts")
def contracts(kind: str = Query(default="futures", pattern="^(futures|options)$"), exchange: str | None = None, segment: str | None = None, instrument_type: str | None = None,
              underlying: str | None = None, expiry: date | None = None, option_type: str | None = Query(default=None, pattern="^(CE|PE)$"), status: str | None = Query(default=None, pattern="^(active|expired|removed)$"),
              limit: int = Query(default=100, ge=1, le=1000), offset: int = Query(default=0, ge=0), _: UserSession = Depends(require_verified_session), db: Session = Depends(get_db)) -> dict[str, Any]:
    model = DerivativeContract if kind == "futures" else OptionContract
    sym = DerivativeContract.underlying_symbol if kind == "futures" else OptionContract.underlying
    stmt = select(model).where(model.valid_to.is_(None))
    for col, val in ((model.exchange, exchange), (model.segment, segment), (model.instrument_type, instrument_type), (sym, underlying), (model.status, status)):
        if val:
            stmt = stmt.where(col == val)
    if kind == "options" and option_type:
        stmt = stmt.where(OptionContract.option_type == option_type)
    if expiry:
        stmt = stmt.where((DerivativeContract.expiry == expiry) if kind == "futures" else (func.date(OptionContract.expiry) == expiry))
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    contracts = list(db.scalars(stmt.order_by(sym, model.expiry).limit(limit).offset(offset)))
    contract_ids = [c.id for c in contracts]
    candle_model = FuturesCandle1m if kind == "futures" else OptionCandle1m
    candle_stats = {r[0]: {"rows": int(r[1]), "first": r[2], "last": r[3], "sessions": int(r[4])} for r in db.execute(
        select(candle_model.contract_id, func.count(), func.min(candle_model.ts), func.max(candle_model.ts), func.count(func.distinct(func.date(candle_model.ts))))
        .where(candle_model.contract_id.in_(contract_ids)).group_by(candle_model.contract_id)
    )} if contract_ids else {}
    task_rows = list(db.scalars(select(DerivativeIngestionTask).where(DerivativeIngestionTask.params["contract_id"].as_string().in_([str(i) for i in contract_ids])))) if contract_ids else []
    task_stats: dict[str, dict[str, Any]] = {}
    for task in task_rows:
        cid = (task.params or {}).get("contract_id")
        if not cid:
            continue
        state = task_stats.setdefault(cid, {"last_success_at": None, "invalid": 0, "warnings": []})
        if task.status in ("done", "empty") and task.finished_at and (state["last_success_at"] is None or task.finished_at > state["last_success_at"]):
            state["last_success_at"] = task.finished_at
        if task.error:
            match = re.search(r"(\d+) invalid provider candle", task.error)
            if match:
                state["invalid"] += int(match.group(1))
            if task.error not in state["warnings"]:
                state["warnings"].append(task.error[:300])
    latest_live: dict[str, OptionQuote] = {}
    if kind == "options" and contract_ids:
        latest = select(OptionQuote.contract_id, func.max(OptionQuote.quote_time).label("quote_time")).where(OptionQuote.contract_id.in_(contract_ids)).group_by(OptionQuote.contract_id).subquery()
        for quote in db.scalars(select(OptionQuote).join(latest, (OptionQuote.contract_id == latest.c.contract_id) & (OptionQuote.quote_time == latest.c.quote_time))):
            latest_live[str(quote.contract_id)] = quote
    items = []
    for c in contracts:
        stats = candle_stats.get(c.id, {"rows": 0, "first": None, "last": None, "sessions": 0})
        task = task_stats.get(str(c.id), {"last_success_at": None, "invalid": 0, "warnings": []})
        missing = None
        if c.segment in ("NSE_FNO", "BSE_FNO"):
            expected = stats["sessions"] * 375
            missing = {"sessions": stats["sessions"], "expected_minutes": expected, "present_minutes": stats["rows"], "estimated_missing_minutes": max(0, expected - stats["rows"]), "note": "Session estimate uses 375 minutes for NSE/BSE; provider candles are completed bars."}
        q = latest_live.get(str(c.id))
        items.append({"id": str(c.id), "exchange": c.exchange, "segment": c.segment, "instrument_type": c.instrument_type, "security_id": c.security_id, "underlying": c.underlying_symbol if kind == "futures" else c.underlying,
                      "trading_symbol": c.trading_symbol, "expiry": (c.expiry if kind == "futures" else c.expiry.date()).isoformat(), "lot_size": str(c.lot_size) if c.lot_size is not None else None,
                      "tick_size": str(c.tick_size) if c.tick_size is not None else None, "status": c.status, "valid_from": c.valid_from.isoformat(), "master_version": c.master_version,
                      "coverage": {"candles_1m": stats["rows"], "first": stats["first"].isoformat() if stats["first"] else None, "last": stats["last"].isoformat() if stats["last"] else None,
                                   "sessions": stats["sessions"], "missing_intervals": missing, "invalid_candles_skipped": task["invalid"], "warnings": task["warnings"],
                                   "last_success_at": task["last_success_at"].isoformat() if task["last_success_at"] else None, "provider": "dhan", "status": "available" if stats["rows"] else "not_ingested",
                                   "latest_live": None if q is None else {"captured_at": q.quote_time.isoformat(), "bid": str(q.bid) if q.bid is not None else None, "ask": str(q.ask) if q.ask is not None else None,
                                                                         "iv": str(q.implied_volatility) if q.implied_volatility is not None else None, "oi": q.open_interest}},
                      **({"strike": str(c.strike), "option_type": c.option_type} if kind == "options" else {})})
    return {"total": total, "items": items}


@router.get("/coverage")
def coverage(exchange: str | None = None, segment: str | None = None, category: str | None = None, data_type: str | None = None, provenance: str | None = Query(default=None, pattern="^(historical|live)$"),
             contract_state: str | None = None, underlying: str | None = None, timeframe: str | None = None, _: UserSession = Depends(require_verified_session), db: Session = Depends(get_db)) -> dict[str, Any]:
    stmt = select(DerivativeDataCoverage)
    for col, val in ((DerivativeDataCoverage.exchange, exchange), (DerivativeDataCoverage.segment, segment), (DerivativeDataCoverage.category, category), (DerivativeDataCoverage.data_type, data_type),
                     (DerivativeDataCoverage.provenance, provenance), (DerivativeDataCoverage.contract_state, contract_state), (DerivativeDataCoverage.underlying_symbol, underlying), (DerivativeDataCoverage.timeframe, timeframe)):
        if val:
            stmt = stmt.where(col == val)
    rows = list(db.scalars(stmt.order_by(DerivativeDataCoverage.data_type, DerivativeDataCoverage.exchange, DerivativeDataCoverage.segment, DerivativeDataCoverage.instrument_type)))
    return {"updated_at": max((r.updated_at for r in rows), default=None) and max(r.updated_at for r in rows).isoformat(), "rows": [
        {"scope": r.scope_key, "data_type": r.data_type, "category": r.category, "exchange": r.exchange, "segment": r.segment, "instrument_type": r.instrument_type, "underlying": r.underlying_symbol,
         "timeframe": r.timeframe, "provenance": r.provenance, "contract_state": r.contract_state, "instruments": r.instruments, "contracts": r.contracts, "rows": r.row_count,
         "first": r.first_ts.isoformat() if r.first_ts else None, "last": r.last_ts.isoformat() if r.last_ts else None, "sessions": r.sessions, "missing": r.missing_intervals,
         "last_success_at": r.last_success_at.isoformat() if r.last_success_at else None, "last_status": r.last_status, "last_error": r.last_error, "provider": r.provider, "blocker": r.blocker,
         "status": "available" if r.row_count else ("blocked" if r.blocker and ("not " in r.blocker or "No " in r.blocker) else "empty")} for r in rows], "disclaimer": DISCLAIMER}


@router.post("/coverage/refresh")
def coverage_refresh(_: UserSession = Depends(require_verified_session), __: UserSession = Depends(require_csrf), db: Session = Depends(get_db)) -> dict[str, Any]:
    return {"rows": di.refresh_coverage(db)}


@router.get("/chain-snapshots")
def chain_snapshots(underlying: str | None = None, exchange: str | None = None, limit: int = Query(default=50, ge=1, le=500), _: UserSession = Depends(require_verified_session),
                    db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    stmt = select(OptionChainSnapshot)
    if underlying:
        stmt = stmt.where(OptionChainSnapshot.underlying_symbol == underlying)
    if exchange:
        stmt = stmt.where(OptionChainSnapshot.exchange == exchange)
    return [{"id": str(s.id), "exchange": s.exchange, "underlying": s.underlying_symbol, "expiry": s.expiry.isoformat(), "underlying_price": str(s.underlying_price) if s.underlying_price is not None else None,
             "captured_at": s.captured_at.isoformat(), "strikes": s.strikes, "quotes_stored": s.quotes_stored, "quotes_with_bid_ask": s.quotes_with_bid_ask}
            for s in db.scalars(stmt.order_by(OptionChainSnapshot.captured_at.desc()).limit(limit))]


@router.get("/jobs")
def jobs(limit: int = Query(default=30, ge=1, le=200), active_session: UserSession = Depends(require_verified_session), db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    rows = list(db.scalars(select(DerivativeIngestionJob).where(DerivativeIngestionJob.user_id == active_session.user_id).order_by(DerivativeIngestionJob.created_at.desc()).limit(limit)))
    if not rows:
        return []
    attempts = dict(db.execute(select(DerivativeIngestionTask.job_id, func.coalesce(func.sum(DerivativeIngestionTask.attempts), 0)).where(DerivativeIngestionTask.job_id.in_([j.id for j in rows])).group_by(DerivativeIngestionTask.job_id)).all())
    return [_job_out(j, int(attempts.get(j.id, 0))) for j in rows]


@router.get("/jobs/{job_id}")
def job_detail(job_id: UUID, active_session: UserSession = Depends(require_verified_session), db: Session = Depends(get_db)) -> dict[str, Any]:
    j = db.scalar(select(DerivativeIngestionJob).where(DerivativeIngestionJob.id == job_id, DerivativeIngestionJob.user_id == active_session.user_id))
    if j is None:
        raise HTTPException(404, "Job not found")
    counts = dict(db.execute(select(DerivativeIngestionTask.status, func.count()).where(DerivativeIngestionTask.job_id == job_id).group_by(DerivativeIngestionTask.status)).all())
    errors = [{"task": t.task_key, "status": t.status, "attempts": t.attempts, "error": t.error} for t in db.scalars(select(DerivativeIngestionTask).where(DerivativeIngestionTask.job_id == job_id,
              DerivativeIngestionTask.status.in_(("dead", "failed"))).limit(50))]
    attempts = db.scalar(select(func.coalesce(func.sum(DerivativeIngestionTask.attempts), 0)).where(DerivativeIngestionTask.job_id == job_id)) or 0
    return {**_job_out(j, int(attempts)), "task_counts": counts, "errors": errors}


@router.post("/jobs")
def create_job(body: JobIn, active_session: UserSession = Depends(require_verified_session), _: UserSession = Depends(require_csrf), db: Session = Depends(get_db)) -> dict[str, Any]:
    """dry_run (the default) returns the request plan and its minimum duration without calling Dhan. A real job runs in the background, rate-limited and resumable."""
    tasks, scope = _plan(db, body)
    est = di.estimate_requests(tasks)
    if body.dry_run:
        return {"dry_run": True, "scope": scope, **est, "message": "Estimate only: nothing was requested. Re-submit with dry_run=false to start."}
    if not tasks:
        raise HTTPException(422, detail="The plan is empty: no matching contracts/underlyings (is the instrument master refreshed, and for near-ATM candles is a chain snapshot stored?).")
    job = di.create_job(db, f"{body.kind}_backfill", {"scope": scope, "estimate": est, "request": body.model_dump(exclude={"dry_run"})}, tasks, user_id=active_session.user_id)
    start_job_thread(job.id, active_session.user_id)
    return {"dry_run": False, "job": _job_out(job), **est}


@router.post("/jobs/{job_id}/cancel")
def cancel(job_id: UUID, active_session: UserSession = Depends(require_verified_session), __: UserSession = Depends(require_csrf), db: Session = Depends(get_db)) -> dict[str, Any]:
    if db.scalar(select(DerivativeIngestionJob.id).where(DerivativeIngestionJob.id == job_id, DerivativeIngestionJob.user_id == active_session.user_id)) is None:
        raise HTTPException(404, "Job not found")
    if not di.cancel_job(db, job_id):
        raise HTTPException(409, "Job is not running or does not exist")
    return {"cancel_requested": True}


@router.post("/jobs/{job_id}/resume")
def resume(job_id: UUID, active_session: UserSession = Depends(require_verified_session), _: UserSession = Depends(require_csrf), db: Session = Depends(get_db)) -> dict[str, Any]:
    j = db.scalar(select(DerivativeIngestionJob).where(DerivativeIngestionJob.id == job_id, DerivativeIngestionJob.user_id == active_session.user_id))
    if j is None:
        raise HTTPException(404, "Job not found")
    if j.status in ("succeeded", "cancelled"):
        raise HTTPException(409, f"Job is {j.status}")
    j.status, j.cancel_requested, j.finished_at = "queued", False, None
    db.commit()
    start_job_thread(job_id, active_session.user_id)
    return {"resumed": True}


@router.post("/futures/{contract_id}/register-instrument")
def register_instrument(contract_id: UUID, _: UserSession = Depends(require_verified_session), __: UserSession = Depends(require_csrf), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Make one futures contract available to the existing live feed and the Signals instrument pickers (idempotent)."""
    try:
        inst = di.register_futures_instrument(db, contract_id)
    except ValueError as e:
        raise HTTPException(404, str(e)) from None
    return {"instrument_id": str(inst.id), "symbol": inst.symbol}
