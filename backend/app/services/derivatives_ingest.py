"""Derivatives ingestion: master refresh (history-preserving), idempotent candle/snapshot stores, resumable job engine, planners and coverage.

Idempotency: every store uses INSERT ... ON CONFLICT DO NOTHING on the natural key, so replaying a window or resuming a job never duplicates rows.
Candles still forming (open time + interval > now) are never stored. Nothing is estimated or spliced.
"""
from __future__ import annotations

import hashlib
import json
import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Iterable
from uuid import UUID, uuid4

from sqlalchemy import Integer, cast, delete, func, insert, literal, select, update
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.orm import Session

from app.broker_adapters import BrokerAdapterError, BrokerRateLimited, BrokerReconnectRequired
from app.models import DerivativeContract, DerivativeDataCoverage, DerivativeIngestionJob, DerivativeIngestionTask, DerivativeInstrument, DerivativeMasterSnapshot
from app.models import FuturesCandle1d, FuturesCandle1m, OptionCandle1m, OptionChainSnapshot, RollingOptionCandle1m
from app.models.strategies import OptionContract, OptionQuote
from app.services import dhan_derivatives as dd

logger = logging.getLogger(__name__)
D = Decimal
CHUNK = 4000
MAX_ATTEMPTS = 3
BACKOFF_SECONDS = (30, 120, 600)
STALE_RUNNING = timedelta(minutes=15)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(ts: datetime | date | None) -> datetime | None:
    if ts is not None and not isinstance(ts, datetime):      # a DATE column (daily candles)
        return datetime(ts.year, ts.month, ts.day, tzinfo=timezone.utc)
    return None if ts is None else (ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts.astimezone(timezone.utc))


def insert_ignore(db: Session, model: Any, rows: list[dict[str, Any]]) -> int:
    """Bulk INSERT ... ON CONFLICT DO NOTHING (postgres and sqlite). Returns the number of rows actually inserted."""
    if not rows:
        return 0
    dialect = db.get_bind().dialect.name
    ins = (postgresql.insert if dialect == "postgresql" else sqlite.insert)
    n = 0
    for i in range(0, len(rows), CHUNK):
        res = db.execute(ins(model).values(rows[i:i + CHUNK]).on_conflict_do_nothing().returning(literal(1)))
        n += len(res.all())
    return n


# ───────────────────────── master refresh ─────────────────────────
def refresh_master(db: Session, parsed: dd.MasterParse, now: datetime | None = None, source_url: str = dd.MASTER_URL) -> dict[str, Any]:
    """Diff the current master against stored contracts. New -> inserted; changed spec -> old row closed (valid_to) and a NEW row opened, so old specifications
    are never overwritten; listed but past expiry -> expired; no longer listed -> removed (or expired when past expiry)."""
    now = now or utcnow()
    today = now.astimezone(dd.IST).date()
    version = f"dhan-master-{now:%Y%m%dT%H%M%SZ}-{parsed.sha256[:10]}"
    last = db.scalar(select(DerivativeMasterSnapshot).order_by(DerivativeMasterSnapshot.fetched_at.desc()).limit(1))
    unchanged = last is not None and last.content_sha256 == parsed.sha256
    # underlyings
    existing_u = {(u.exchange, u.master_segment, u.underlying_symbol): u for u in db.scalars(select(DerivativeInstrument))}
    new_u = 0
    for key, v in parsed.underlyings.items():
        u = existing_u.get(key)
        if u is None:
            u = DerivativeInstrument(id=uuid4(), first_seen_at=now, last_seen_at=now, status="active", **v)
            db.add(u)
            existing_u[key] = u
            new_u += 1
        else:
            u.last_seen_at, u.status = now, "active"
            for f in ("underlying_security_id", "cash_security_id", "cash_segment"):
                if v.get(f) and getattr(u, f) != v[f]:
                    setattr(u, f, v[f])
            u.has_futures, u.has_options = u.has_futures or v["has_futures"], u.has_options or v["has_options"]
    db.flush()
    inst_id = {k: u.id for k, u in existing_u.items()}
    stats = {"new": 0, "changed": 0, "expired": 0, "removed": 0}
    seen: dict[str, set[tuple[str, str, str]]] = {"fut": set(), "opt": set()}
    for kind, model in (("fut", DerivativeContract), ("opt", OptionContract)):
        cur = {}
        for r in db.execute(select(model.id, model.exchange, model.segment, model.security_id, model.spec_hash, model.status).where(model.valid_to.is_(None))):
            cur[(r.exchange, r.segment, r.security_id)] = r
        rows_new: list[dict[str, Any]] = []
        close_ids: list[UUID] = []
        touch_ids: list[UUID] = []
        for c in parsed.contracts:
            if c.is_option != (kind == "opt"):
                continue
            key = (c.exchange, c.segment, c.security_id)
            seen[kind].add(key)
            prev = cur.get(key)
            if prev is not None and prev.spec_hash == c.spec_hash:
                touch_ids.append(prev.id)
                continue
            if prev is not None:
                close_ids.append(prev.id)
                stats["changed"] += 1
            else:
                stats["new"] += 1
            base = dict(id=uuid4(), exchange=c.exchange, segment=c.segment, security_id=c.security_id, instrument_type=c.instrument_type, underlying_security_id=c.underlying_security_id,
                        tick_size=c.tick_size, status="expired" if c.expiry < today else "active", expiry_flag=c.expiry_flag, valid_from=now, valid_to=None,
                        first_seen_at=now, last_seen_at=now, spec_hash=c.spec_hash, source="dhan_master", master_version=version, ingested_at=now,
                        derivative_instrument_id=inst_id.get((c.exchange, c.master_segment, c.underlying_symbol)))
            if kind == "fut":
                rows_new.append({**base, "master_segment": c.master_segment, "underlying_symbol": c.underlying_symbol, "trading_symbol": c.trading_symbol, "display_name": c.display_name, "expiry": c.expiry,
                                 "lot_size": c.lot_size})
            else:
                rows_new.append({**base, "underlying": c.underlying_symbol, "trading_symbol": c.trading_symbol, "provider_id": f"{c.security_id}|{c.segment}", "expiry": c.expiry_at, "strike": c.strike,
                                 "option_type": c.option_type, "lot_size": int(c.lot_size) if c.lot_size is not None else None, "instrument_id": None})
        if kind == "fut":
            for r in rows_new:
                r.pop("derivative_instrument_id", None)
                r["derivative_instrument_id"] = inst_id.get((r["exchange"], r["master_segment"], r["underlying_symbol"]))
        for i in range(0, len(close_ids), CHUNK):
            db.execute(update(model).where(model.id.in_(close_ids[i:i + CHUNK])).values(valid_to=now))
        for i in range(0, len(touch_ids), CHUNK):
            db.execute(update(model).where(model.id.in_(touch_ids[i:i + CHUNK])).values(last_seen_at=now))
        for i in range(0, len(rows_new), CHUNK):
            db.execute(insert(model), rows_new[i:i + CHUNK])
        # contracts previously stored but no longer listed
        gone = [r for k, r in cur.items() if k not in seen[kind]]
        for i in range(0, len(gone), CHUNK):
            ids = [g.id for g in gone[i:i + CHUNK]]
            db.execute(update(model).where(model.id.in_(ids), model.status == "active").values(status="removed", last_seen_at=now))
        stats["removed"] += len(gone)
        # active rows whose expiry has passed
        expiry_cut = today if kind == "fut" else datetime(today.year, today.month, today.day, tzinfo=dd.IST).astimezone(timezone.utc)
        res = db.execute(update(model).where(model.status == "active", model.valid_to.is_(None), model.expiry < expiry_cut).values(status="expired"))
        stats["expired"] += max(res.rowcount or 0, 0)
    snap = DerivativeMasterSnapshot(id=uuid4(), fetched_at=now, source_url=source_url, content_sha256=parsed.sha256, rows_total=parsed.rows_total, derivative_rows=len(parsed.contracts),
                                    new_contracts=stats["new"], changed_contracts=stats["changed"], expired_contracts=stats["expired"], removed_contracts=stats["removed"],
                                    detail={"unchanged_since_last_snapshot": unchanged, "new_underlyings": new_u, "skipped_rows": parsed.skipped, "master_version": version})
    db.add(snap)
    db.commit()
    return {"snapshot_id": str(snap.id), "master_version": version, "derivative_rows": len(parsed.contracts), "underlyings": len(parsed.underlyings), "new_underlyings": new_u,
            **stats, "unchanged": unchanged, "skipped": parsed.skipped}


# ───────────────────────── stores ─────────────────────────
def store_futures_candles(db: Session, contract_id: UUID, rows: list[dd.CandleRow], now: datetime, source: str = "dhan_intraday") -> int:
    done = [r for r in rows if r.ts + timedelta(minutes=1) <= now]            # forming minute excluded
    return insert_ignore(db, FuturesCandle1m, [dict(contract_id=contract_id, ts=r.ts, open=r.open, high=r.high, low=r.low, close=r.close, volume=r.volume, open_interest=r.open_interest, source=source,
                                                    retrieved_at=now) for r in done])


def store_option_candles(db: Session, contract_id: UUID, rows: list[dd.CandleRow], now: datetime, source: str = "dhan_intraday") -> int:
    done = [r for r in rows if r.ts + timedelta(minutes=1) <= now]
    return insert_ignore(db, OptionCandle1m, [dict(contract_id=contract_id, ts=r.ts, open=r.open, high=r.high, low=r.low, close=r.close, volume=r.volume, open_interest=r.open_interest, source=source,
                                                   retrieved_at=now) for r in done])


def store_daily(db: Session, *, exchange: str, segment: str, security_id: str, series_kind: str, expiry_code: int, contract_id: UUID | None, rows: list[dd.CandleRow], now: datetime) -> int:
    out = []
    for r in rows:
        day = r.ts.astimezone(dd.IST).date()                                     # Dhan daily epochs are IST midnight instants
        out.append(dict(contract_id=contract_id, exchange=exchange, segment=segment, security_id=security_id, series_kind=series_kind, expiry_code=expiry_code, day=day, open=r.open, high=r.high,
                        low=r.low, close=r.close, volume=r.volume, open_interest=r.open_interest, source="dhan_historical", retrieved_at=now))
    return insert_ignore(db, FuturesCandle1d, out)


def store_rolling(db: Session, *, exchange: str, segment: str, cash_security_id: str, symbol: str, instrument: str, expiry_flag: str, expiry_code: int, strike_offset: str, option_type: str,
                  rows: list[dd.RollingRow], request: dict[str, Any], now: datetime) -> int:
    """Stored AS rolling-strike data: ``strike_offset`` is part of the key and the provider's absolute ``strike`` is kept per candle. Never mapped to a fixed contract here."""
    base = dict(exchange=exchange, segment=segment, underlying_security_id=cash_security_id, underlying_symbol=symbol, instrument_type=instrument, expiry_flag=expiry_flag, expiry_code=expiry_code,
                strike_offset=strike_offset, option_type=option_type, source="dhan_rollingoption", request=request, retrieved_at=now)
    return insert_ignore(db, RollingOptionCandle1m, [dict(base, ts=r.ts, open=r.open, high=r.high, low=r.low, close=r.close, volume=r.volume, open_interest=r.open_interest, implied_volatility=r.iv,
                                                         spot=r.spot, strike=r.strike) for r in rows])


def store_chain_snapshot(db: Session, *, underlying: DerivativeInstrument, expiry: date, price: Decimal | None, quotes: list[dd.ChainQuote], captured_at: datetime) -> dict[str, int]:
    """Persist one live chain. Quotes are linked to option_contracts by the provider's security id; a quote whose contract is not in the master is COUNTED as unmatched, never invented."""
    contracts = {c.security_id: c.id for c in db.scalars(select(OptionContract).where(OptionContract.exchange == underlying.exchange, OptionContract.underlying == underlying.underlying_symbol,
                                                                                         OptionContract.valid_to.is_(None), OptionContract.status == "active"))
                 if c.expiry.astimezone(dd.IST).date() == expiry}
    snap = OptionChainSnapshot(id=uuid4(), derivative_instrument_id=underlying.id, exchange=underlying.exchange, underlying_symbol=underlying.underlying_symbol,
                               underlying_security_id=underlying.cash_security_id or "", underlying_segment=underlying.cash_segment or underlying.segment, expiry=expiry, underlying_price=price,
                               captured_at=captured_at, strikes=len({q.strike for q in quotes}), quotes_stored=0, quotes_with_bid_ask=0)
    db.add(snap)
    db.flush()
    rows, unmatched, with_ba = [], 0, 0
    for q in quotes:
        cid = contracts.get(q.security_id or "")
        if cid is None:
            unmatched += 1
            continue
        if q.bid is not None and q.ask is not None:
            with_ba += 1
        rows.append(dict(id=uuid4(), contract_id=cid, quote_time=captured_at, ingested_at=captured_at, bid=q.bid, ask=q.ask, bid_qty=q.bid_qty, ask_qty=q.ask_qty, ltp=q.ltp, volume=q.volume,
                         open_interest=q.oi, implied_volatility=q.iv, delta=q.delta, greeks_source="provider" if q.delta is not None else None, underlying_price=price, source="dhan_optionchain",
                         snapshot_id=snap.id, gamma=q.gamma, theta=q.theta, vega=q.vega, previous_open_interest=q.previous_oi, previous_volume=q.previous_volume))
    stored = insert_ignore(db, OptionQuote, rows)
    snap.quotes_stored, snap.quotes_with_bid_ask = stored, with_ba
    db.flush()
    return {"snapshot_id": str(snap.id), "quotes": len(quotes), "stored": stored, "unmatched_to_master": unmatched, "with_bid_ask": with_ba}


# ───────────────────────── jobs ─────────────────────────
@dataclass
class TaskSpec:
    key: str
    kind: str
    params: dict[str, Any]


def create_job(db: Session, kind: str, params: dict[str, Any], tasks: list[TaskSpec], *, priority: int = 50, user_id: UUID | None = None) -> DerivativeIngestionJob:
    now = utcnow()
    job = DerivativeIngestionJob(id=uuid4(), kind=kind, status="queued", priority=priority, params=params, tasks_total=len(tasks), created_at=now, user_id=user_id)
    db.add(job)
    db.flush()
    seen: set[str] = set()
    rows = []
    for t in tasks:
        if t.key in seen:
            continue
        seen.add(t.key)
        rows.append(dict(job_id=job.id, task_key=t.key[:240], kind=t.kind, status="pending", params=t.params, attempts=0, rows=0))
    for i in range(0, len(rows), CHUNK):
        db.execute(insert(DerivativeIngestionTask), rows[i:i + CHUNK])
    job.tasks_total = len(rows)
    db.commit()
    return job


def cancel_job(db: Session, job_id: UUID) -> bool:
    job = db.get(DerivativeIngestionJob, job_id)
    if job is None or job.status in ("succeeded", "partial", "failed", "cancelled"):
        return False
    job.cancel_requested = True
    if job.status == "queued":
        job.status, job.finished_at = "cancelled", utcnow()
        db.execute(update(DerivativeIngestionTask).where(DerivativeIngestionTask.job_id == job.id, DerivativeIngestionTask.status == "pending").values(status="cancelled"))
    db.commit()
    return True


def _classify(error: Exception) -> tuple[bool, str]:
    """(retryable, message). 400-class provider validation errors are permanent; rate limits, auth renewals, network and 5xx are retried."""
    if isinstance(error, BrokerRateLimited):
        return True, "rate limited"
    if isinstance(error, BrokerReconnectRequired):
        return True, "Dhan session needs renewal"
    if isinstance(error, BrokerAdapterError):
        sc = getattr(error, "status_code", None)
        return (sc is None or sc >= 500), f"{error} (status {sc})"
    if isinstance(error, dd.PayloadError):
        return False, f"unexpected payload: {error}"
    return False, f"{type(error).__name__}: {error}"


Executor = Callable[[Session, DerivativeIngestionTask, Any, datetime], tuple[int, str]]


def execute_task(db: Session, task: DerivativeIngestionTask, client: Any, now: datetime) -> tuple[int, str]:
    """Run one task. Returns (rows_written, 'done'|'empty'). Raises on provider failure."""
    p = task.params
    if task.kind in ("futures_intraday", "option_intraday"):
        model = DerivativeContract if task.kind == "futures_intraday" else OptionContract
        c = db.get(model, UUID(p["contract_id"]))
        if c is None:
            raise ValueError("contract not found")
        rows = client.intraday(segment=c.segment, security_id=c.security_id, instrument=c.instrument_type, start=datetime.fromisoformat(p["start"]), end=datetime.fromisoformat(p["end"]), oi=True)
        n = (store_futures_candles if task.kind == "futures_intraday" else store_option_candles)(db, c.id, rows, now)
        task.error = f"note: {rows.rejected} invalid provider candle(s) skipped" if getattr(rows, "rejected", 0) else None
        return n, "done" if rows else "empty"
    if task.kind == "futures_daily":
        c = db.get(DerivativeContract, UUID(p["contract_id"]))
        if c is None:
            raise ValueError("contract not found")
        rows = client.daily(segment=c.segment, security_id=c.security_id, instrument=c.instrument_type, start=date.fromisoformat(p["start"]), end=date.fromisoformat(p["end"]), expiry_code=int(p.get("expiry_code", 0)))
        kind = "contract" if int(p.get("expiry_code", 0)) == 0 and p.get("series_kind") != "provider_rolled" else "provider_rolled"
        n = store_daily(db, exchange=c.exchange, segment=c.segment, security_id=c.security_id, series_kind=kind, expiry_code=int(p.get("expiry_code", 0)), contract_id=c.id if kind == "contract" else None, rows=rows, now=now)
        task.error = f"note: {rows.rejected} invalid provider candle(s) skipped: {rows.rejected_examples}" if getattr(rows, "rejected", 0) else None
        return n, "done" if rows else "empty"
    if task.kind == "rolling":
        rows = client.rolling(segment=p["segment"], cash_security_id=p["cash_security_id"], instrument=p["instrument"], expiry_flag=p["expiry_flag"], expiry_code=int(p["expiry_code"]), strike=p["strike_offset"],
                              option_type=p["option_type"], first=date.fromisoformat(p["from"]), last=date.fromisoformat(p["to"]), interval="1")
        req = {k: p[k] for k in ("segment", "cash_security_id", "instrument", "expiry_flag", "expiry_code", "strike_offset", "option_type", "from", "to")} | {"interval": "1"}
        n = store_rolling(db, exchange=p["exchange"], segment=p["segment"], cash_security_id=p["cash_security_id"], symbol=p["symbol"], instrument=p["instrument"], expiry_flag=p["expiry_flag"],
                          expiry_code=int(p["expiry_code"]), strike_offset=p["strike_offset"], option_type=p["option_type"], rows=rows, request=req, now=now)
        return n, "done" if rows else "empty"
    if task.kind == "master_refresh":
        res = refresh_master(db, dd.parse_master(dd.fetch_master_lines()), now)
        task.error = f"note: {res}"[:480]
        return res["new"] + res["changed"], "done"
    if task.kind == "chain_snapshot":
        u = db.get(DerivativeInstrument, UUID(p["instrument_id"]))
        if u is None or not u.cash_security_id or not u.cash_segment:
            raise ValueError("underlying has no resolvable cash/index security id")
        expiry = date.fromisoformat(p["expiry"])
        price, quotes = client.chain(u.cash_security_id, u.cash_segment, expiry)
        res = store_chain_snapshot(db, underlying=u, expiry=expiry, price=price, quotes=quotes, captured_at=now)
        return res["stored"], "done" if quotes else "empty"
    raise ValueError(f"unknown task kind {task.kind!r}")


def run_job(session_factory: Callable[[], Session], job_id: UUID, client: Any, *, now_fn: Callable[[], datetime] = utcnow, sleep: Callable[[float], None] | None = None, max_tasks: int | None = None) -> str:
    """Process a job's pending/retryable tasks sequentially (the provider limits requests per second anyway). Resumable: done/empty tasks are never repeated."""
    processed = 0
    while True:
        with session_factory() as db:
            job = db.get(DerivativeIngestionJob, job_id)
            if job is None:
                return "missing"
            if job.status == "queued":
                job.status, job.started_at = "running", now_fn()
            now = now_fn()
            if job.cancel_requested:
                db.execute(update(DerivativeIngestionTask).where(DerivativeIngestionTask.job_id == job_id, DerivativeIngestionTask.status.in_(("pending", "failed"))).values(status="cancelled"))
                job.status, job.finished_at = "cancelled", now
                db.commit()
                return "cancelled"
            stale = now - STALE_RUNNING
            task = db.scalar(select(DerivativeIngestionTask).where(
                DerivativeIngestionTask.job_id == job_id,
                ((DerivativeIngestionTask.status == "pending") | ((DerivativeIngestionTask.status == "failed") & (DerivativeIngestionTask.attempts < MAX_ATTEMPTS)
                                                                   & ((DerivativeIngestionTask.next_attempt_at.is_(None)) | (DerivativeIngestionTask.next_attempt_at <= now)))
                 | ((DerivativeIngestionTask.status == "running") & (DerivativeIngestionTask.started_at < stale)))).order_by(DerivativeIngestionTask.id).limit(1))
            if task is None or (max_tasks is not None and processed >= max_tasks):
                waiting = db.scalar(select(func.count()).select_from(DerivativeIngestionTask).where(DerivativeIngestionTask.job_id == job_id, DerivativeIngestionTask.status == "failed",
                                                                                                 DerivativeIngestionTask.attempts < MAX_ATTEMPTS)) or 0
                if task is None and not waiting:
                    return _finish(db, job)
                db.commit()
                if task is None and waiting and sleep is not None:
                    sleep(1.0)
                    continue
                return job.status
            task.status, task.started_at, task.attempts = "running", now, task.attempts + 1
            db.commit()
            tid = task.id
        with session_factory() as db:
            task = db.get(DerivativeIngestionTask, tid)
            job = db.get(DerivativeIngestionJob, job_id)
            try:
                rows, status = execute_task(db, task, client, now_fn())
                task.status, task.rows, task.finished_at = status, rows, now_fn()
                job.tasks_done += 1
                job.rows_written += rows
                db.commit()
            except Exception as e:  # noqa: BLE001 - recorded on the task; the job continues
                db.rollback()
                task = db.get(DerivativeIngestionTask, tid)
                job = db.get(DerivativeIngestionJob, job_id)
                retry, msg = _classify(e)
                task.error = msg[:500]
                if retry and task.attempts < MAX_ATTEMPTS:
                    task.status = "failed"
                    task.next_attempt_at = now_fn() + timedelta(seconds=BACKOFF_SECONDS[min(task.attempts - 1, len(BACKOFF_SECONDS) - 1)])
                else:
                    task.status = "dead"
                    job.tasks_failed += 1
                task.finished_at = now_fn()
                db.commit()
        processed += 1


def _finish(db: Session, job: DerivativeIngestionJob) -> str:
    counts = dict(db.execute(select(DerivativeIngestionTask.status, func.count()).where(DerivativeIngestionTask.job_id == job.id).group_by(DerivativeIngestionTask.status)).all())
    dead = counts.get("dead", 0)
    ok = counts.get("done", 0) + counts.get("empty", 0)
    job.status = "succeeded" if dead == 0 else ("failed" if ok == 0 else "partial")
    job.finished_at = utcnow()
    job.tasks_done = ok
    job.tasks_failed = dead
    if dead:
        job.error = f"{dead} task(s) failed permanently; see task errors"
    db.commit()
    return job.status


# ───────────────────────── planners ─────────────────────────
def _contract_query(model: Any, scope: dict[str, Any]):
    q = select(model).where(model.status == "active", model.valid_to.is_(None))
    if scope.get("exchange"):
        q = q.where(model.exchange == scope["exchange"])
    if scope.get("segment"):
        q = q.where(model.segment == scope["segment"])
    if scope.get("instrument_type"):
        q = q.where(model.instrument_type == scope["instrument_type"])
    if scope.get("underlying"):
        q = q.where((model.underlying_symbol if model is DerivativeContract else model.underlying) == scope["underlying"])
    return q


def plan_futures(db: Session, scope: dict[str, Any], *, lookback_days: int = 100, now: datetime | None = None, daily: bool = True) -> list[TaskSpec]:
    now = now or utcnow()
    tasks: list[TaskSpec] = []
    daily_done: set[tuple[str, str, str]] = set()
    limit = int(scope.get("limit", 1000))
    for c in db.scalars(_contract_query(DerivativeContract, scope).order_by(DerivativeContract.underlying_symbol, DerivativeContract.expiry).limit(limit)):
        if c.segment not in dd.CAPABILITIES["futures_intraday_1m"]["segments"]:
            continue
        for a, b in dd.intraday_windows(now - timedelta(days=lookback_days), now):
            tasks.append(TaskSpec(f"fut1m:{c.id}:{a:%Y%m%d}:{b:%Y%m%d}", "futures_intraday", {"contract_id": str(c.id), "start": a.isoformat(), "end": b.isoformat()}))
        if daily and (c.exchange, c.instrument_type, c.underlying_symbol) not in daily_done:     # the provider-rolled daily series is per underlying: one request, not one per expiry
            daily_done.add((c.exchange, c.instrument_type, c.underlying_symbol))
            tasks.append(TaskSpec(f"fut1d:{c.id}:provider", "futures_daily", {"contract_id": str(c.id), "start": (now - timedelta(days=1000)).date().isoformat(), "end": now.date().isoformat(),
                                                                                "expiry_code": 0, "series_kind": "provider_rolled"}))
    return tasks


def plan_option_candles(db: Session, scope: dict[str, Any], *, lookback_days: int = 10, now: datetime | None = None) -> list[TaskSpec]:
    now = now or utcnow()
    tasks: list[TaskSpec] = []
    for c in db.scalars(_contract_query(OptionContract, scope).order_by(OptionContract.underlying, OptionContract.expiry, OptionContract.strike).limit(int(scope.get("limit", 500)))):
        if c.segment not in dd.CAPABILITIES["options_active_intraday_1m"]["segments"]:
            continue
        for a, b in dd.intraday_windows(now - timedelta(days=lookback_days), now):
            tasks.append(TaskSpec(f"opt1m:{c.id}:{a:%Y%m%d}:{b:%Y%m%d}", "option_intraday", {"contract_id": str(c.id), "start": a.isoformat(), "end": b.isoformat()}))
    return tasks


def rolling_support(segment: str, instrument: str) -> list[str] | None:
    """Expiry flags the API was MEASURED to serve for this segment/instrument, or None when unsupported."""
    cap = dd.CAPABILITIES["options_rolling_1m"]
    if instrument not in cap["segments"].get(segment, []):
        return None
    return cap["expiry_flags"][instrument]


def plan_rolling(db: Session, scope: dict[str, Any], *, first: date, last: date, max_offset: int = 2, expiry_codes: tuple[int, ...] = (1,), option_types: tuple[str, ...] = ("CE", "PE")) -> list[TaskSpec]:
    first = max(first, dd.ROLLING_EARLIEST)
    tasks: list[TaskSpec] = []
    q = select(DerivativeInstrument).where(DerivativeInstrument.has_options.is_(True), DerivativeInstrument.status == "active")
    if scope.get("exchange"):
        q = q.where(DerivativeInstrument.exchange == scope["exchange"])
    if scope.get("underlying"):
        q = q.where(DerivativeInstrument.underlying_symbol == scope["underlying"])
    if scope.get("category"):
        q = q.where(DerivativeInstrument.category == scope["category"])
    for u in db.scalars(q.order_by(DerivativeInstrument.exchange, DerivativeInstrument.underlying_symbol).limit(int(scope.get("limit", 100)))):
        types = {c for c in db.scalars(select(OptionContract.instrument_type).where(OptionContract.derivative_instrument_id == u.id).distinct())}
        for ins in sorted(types):
            flags = rolling_support(u.segment, ins)
            if not flags or not u.cash_security_id:
                continue
            for flag in flags:
                for code in expiry_codes:
                    for off in dd.strike_offsets(max_offset):
                        for ot in option_types:
                            for a, b in dd.rolling_windows(first, last):
                                tasks.append(TaskSpec(f"roll:{u.exchange}:{u.underlying_symbol}:{ins}:{flag}:{code}:{off}:{ot}:{a:%Y%m%d}", "rolling",
                                                      {"exchange": u.exchange, "segment": u.segment, "cash_security_id": u.cash_security_id, "symbol": u.underlying_symbol, "instrument": ins,
                                                       "expiry_flag": flag, "expiry_code": code, "strike_offset": off, "option_type": ot, "from": a.isoformat(), "to": b.isoformat()}))
    return tasks


def estimate_requests(tasks: list[TaskSpec], per_second: float = 1.0) -> dict[str, Any]:
    by = defaultdict(int)
    for t in tasks:
        by[t.kind] += 1
    chain = by.get("chain_snapshot", 0)
    seconds = (len(tasks) - chain) / per_second + chain * dd.CHAIN_MIN_INTERVAL_SECONDS
    return {"requests": len(tasks), "by_kind": dict(by), "min_seconds": int(seconds), "min_hours": round(seconds / 3600, 2),
            "note": "Lower bound at the provider rate limit (1 data request/s, 1 option-chain request per 3 s) with no retries."}


def plan_chains(db: Session, *, priority_underlyings: Iterable[str] = (), expiries: dict[tuple[str, str], list[date]] | None = None, expiry_count: int = 2, limit: int = 200) -> list[TaskSpec]:
    """Chain polling plan ordered by priority: strategy/watchlist underlyings first, then the rest. ``expiries`` maps (exchange, symbol) to known expiry dates (from the expiry-list API)."""
    pri = {s.upper(): i for i, s in enumerate(priority_underlyings)}
    us = list(db.scalars(select(DerivativeInstrument).where(DerivativeInstrument.has_options.is_(True), DerivativeInstrument.cash_security_id.is_not(None), DerivativeInstrument.status == "active")))
    us.sort(key=lambda u: (pri.get(u.underlying_symbol.upper(), 10_000), u.exchange, u.underlying_symbol))
    tasks = []
    for u in us[:limit]:
        exps = (expiries or {}).get((u.exchange, u.underlying_symbol))
        if exps is None:
            exps = sorted({c.expiry.astimezone(dd.IST).date() for c in db.scalars(select(OptionContract).where(OptionContract.derivative_instrument_id == u.id, OptionContract.status == "active",
                                                                                                                OptionContract.valid_to.is_(None)))})
        for e in exps[:expiry_count]:
            tasks.append(TaskSpec(f"chain:{u.id}:{e}:{utcnow():%Y%m%d%H%M}", "chain_snapshot", {"instrument_id": str(u.id), "expiry": e.isoformat(), "priority": pri.get(u.underlying_symbol.upper(), None)}))
    return tasks


# ───────────────────────── inventory & coverage ─────────────────────────
def inventory(db: Session) -> dict[str, Any]:
    out: dict[str, Any] = {"underlyings": [], "contracts": []}
    for r in db.execute(select(DerivativeInstrument.exchange, DerivativeInstrument.segment, DerivativeInstrument.category, func.count(), func.sum(cast(DerivativeInstrument.has_futures, Integer)),
                               func.sum(cast(DerivativeInstrument.has_options, Integer))).group_by(DerivativeInstrument.exchange, DerivativeInstrument.segment, DerivativeInstrument.category)):
        out["underlyings"].append({"exchange": r[0], "segment": r[1], "category": r[2], "underlyings": r[3], "with_futures": int(r[4] or 0), "with_options": int(r[5] or 0)})
    for model, label in ((DerivativeContract, "futures"), (OptionContract, "options")):
        for r in db.execute(select(model.exchange, model.segment, model.instrument_type, model.status, func.count(), func.count(func.distinct(model.underlying_symbol if model is DerivativeContract else model.underlying)))
                            .where(model.valid_to.is_(None)).group_by(model.exchange, model.segment, model.instrument_type, model.status)):
            out["contracts"].append({"kind": label, "exchange": r[0], "segment": r[1], "instrument_type": r[2], "status": r[3], "contracts": r[4], "underlyings": r[5]})
    last = db.scalar(select(DerivativeMasterSnapshot).order_by(DerivativeMasterSnapshot.fetched_at.desc()).limit(1))
    out["master"] = None if last is None else {"fetched_at": last.fetched_at.isoformat(), "rows_total": last.rows_total, "derivative_rows": last.derivative_rows, "new": last.new_contracts, "changed": last.changed_contracts,
                                                "expired": last.expired_contracts, "removed": last.removed_contracts, "master_version": (last.detail or {}).get("master_version")}
    return out


def _span(db: Session, model: Any, ts_col: Any, *extra: Any) -> tuple[int, datetime | None, datetime | None, int]:
    r = db.execute(select(func.count(), func.min(ts_col), func.max(ts_col), func.count(func.distinct(func.date(ts_col)))).where(*extra)).one()
    return int(r[0]), _aware(r[1]), _aware(r[2]), int(r[3])


def refresh_coverage(db: Session, now: datetime | None = None) -> int:
    """Recompute derivative_data_coverage from stored rows + latest job outcomes. One row per (data type, exchange, segment, instrument type)."""
    now = now or utcnow()
    rows: list[dict[str, Any]] = []

    tasks_all = list(db.scalars(select(DerivativeIngestionTask).where(DerivativeIngestionTask.finished_at.is_not(None)).order_by(DerivativeIngestionTask.finished_at.desc()).limit(50000)))
    contract_scope: dict[str, tuple[str, str, str]] = {}
    cids = {t.params.get("contract_id") for t in tasks_all if t.params and t.params.get("contract_id")}
    for model in (DerivativeContract, OptionContract):
        ids = [UUID(c) for c in cids]
        for i in range(0, len(ids), 2000):
            for r in db.execute(select(model.id, model.exchange, model.segment, model.instrument_type).where(model.id.in_(ids[i:i + 2000]))):
                contract_scope[str(r.id)] = (r.exchange, r.segment, r.instrument_type)
    inst_scope = {str(u.id): (u.exchange, u.segment) for u in db.scalars(select(DerivativeInstrument).where(DerivativeInstrument.has_options.is_(True)))}

    def last_job(kind_prefix: str, exch: str, seg: str, ins: str) -> tuple[datetime | None, str | None, str | None]:
        latest = None
        ok = None
        for t in tasks_all:
            if not t.kind.startswith(kind_prefix) or not t.params:
                continue
            p = t.params
            sc = contract_scope.get(p.get("contract_id", ""))
            match = (sc == (exch, seg, ins)) if sc else ((p.get("exchange"), p.get("segment"), p.get("instrument")) == (exch, seg, ins) if "instrument" in p else (inst_scope.get(p.get("instrument_id", "")) == (exch, seg)))
            if not match:
                continue
            latest = latest or t
            if t.status in ("done", "empty") and ok is None:
                ok = _aware(t.finished_at)
        return ok, (latest.status if latest else None), (latest.error if latest else None)

    def add(key: str, data_type: str, category: str, exch: str, seg: str, ins: str, sym: str | None, tf: str, prov: str, state: str, instruments: int, contracts: int, n: int,
            first: datetime | None, last: datetime | None, sessions: int, capability: str | None, task_kind: str | None, missing: dict | None = None) -> None:
        cap = CAP_FOR.get(capability or "", None)
        cat = category
        ok, status, err = last_job(task_kind, exch, seg, ins) if task_kind else (None, None, None)
        blocker = None
        if cap and cap["status"] in ("unavailable", "unsupported"):
            blocker = cap["note"]
        elif cat == "currency" and n == 0 and capability in ("fut1m", "fut1d", "opt1m", "rolling"):
            blocker = "Currency derivatives are in the master but their Dhan history endpoints were not verified (no active liquid currency contract could be tested)."
        elif n == 0 and cap and ins not in (cap.get("instruments") or [ins]):
            blocker = f"{ins} is not served by {cap['endpoint']}"
        rows.append(dict(id=uuid4(), scope_key=key, data_type=data_type, category=category, exchange=exch, segment=seg, instrument_type=ins, underlying_symbol=sym, timeframe=tf, provenance=prov,
                         contract_state=state, instruments=instruments, contracts=contracts, row_count=n, first_ts=first, last_ts=last, sessions=sessions, missing_intervals=missing, last_success_at=ok,
                         last_status=status, last_error=err, provider="dhan", blocker=blocker, updated_at=now))

    CAP_FOR = {"fut1m": CAPABILITIES_ALIAS["futures_intraday_1m"], "fut1d": CAPABILITIES_ALIAS["futures_daily"], "opt1m": CAPABILITIES_ALIAS["options_active_intraday_1m"],
               "rolling": CAPABILITIES_ALIAS["options_rolling_1m"], "chain": CAPABILITIES_ALIAS["option_chain_live"], "ba": CAPABILITIES_ALIAS["historical_bid_ask"], "futexp": CAPABILITIES_ALIAS["futures_expired"]}
    combos = db.execute(select(DerivativeContract.exchange, DerivativeContract.segment, DerivativeContract.instrument_type, func.count(), func.count(func.distinct(DerivativeContract.underlying_symbol)))
                        .where(DerivativeContract.valid_to.is_(None)).group_by(DerivativeContract.exchange, DerivativeContract.segment, DerivativeContract.instrument_type)).all()
    for exch, seg, ins, n_contracts, n_und in combos:
        cat = dd.CATEGORY_OF[ins]
        n, f, l, s = _span(db, FuturesCandle1m, FuturesCandle1m.ts, FuturesCandle1m.contract_id.in_(select(DerivativeContract.id).where(DerivativeContract.exchange == exch, DerivativeContract.segment == seg,
                                                                                                                                       DerivativeContract.instrument_type == ins)))
        n_c = db.scalar(select(func.count(func.distinct(FuturesCandle1m.contract_id))).where(FuturesCandle1m.contract_id.in_(select(DerivativeContract.id).where(DerivativeContract.exchange == exch,
                                                                                                                                                      DerivativeContract.segment == seg, DerivativeContract.instrument_type == ins)))) or 0
        miss = {"expected_minutes_per_session": 375, "expected_total": s * 375 * max(n_c, 1) if n else 0, "present": n, "note": "estimate for NSE/BSE sessions; per-contract gap detail via /derivatives/contracts/{id}/coverage"} if seg in ("NSE_FNO", "BSE_FNO") else None
        add(f"fut1m:{exch}:{seg}:{ins}", "futures_candles_1m", cat, exch, seg, ins, None, "1m", "historical", "active", int(n_und), int(n_contracts), n, f, l, s, "fut1m", "futures_intraday", miss)
        n, f, l, s = _span(db, FuturesCandle1d, FuturesCandle1d.day, FuturesCandle1d.exchange == exch, FuturesCandle1d.segment == seg,
                           FuturesCandle1d.security_id.in_(select(DerivativeContract.security_id).where(DerivativeContract.exchange == exch, DerivativeContract.segment == seg, DerivativeContract.instrument_type == ins)))
        add(f"fut1d:{exch}:{seg}:{ins}", "futures_candles_1d", cat, exch, seg, ins, None, "1d", "historical", "active", int(n_und), int(n_contracts), n, f, l, s, "fut1d", "futures_daily")
        add(f"futexp:{exch}:{seg}:{ins}", "futures_expired_1m", cat, exch, seg, ins, None, "1m", "historical", "expired", int(n_und), 0, 0, None, None, 0, "futexp", None)
    ocombos = db.execute(select(OptionContract.exchange, OptionContract.segment, OptionContract.instrument_type, func.count(), func.count(func.distinct(OptionContract.underlying)))
                         .where(OptionContract.valid_to.is_(None)).group_by(OptionContract.exchange, OptionContract.segment, OptionContract.instrument_type)).all()
    for exch, seg, ins, n_contracts, n_und in ocombos:
        cat = dd.CATEGORY_OF.get(ins or "", "index")
        ids = select(OptionContract.id).where(OptionContract.exchange == exch, OptionContract.segment == seg, OptionContract.instrument_type == ins)
        n, f, l, s = _span(db, OptionCandle1m, OptionCandle1m.ts, OptionCandle1m.contract_id.in_(ids))
        add(f"opt1m:{exch}:{seg}:{ins}", "option_candles_1m_active", cat, exch, seg, ins or "", None, "1m", "historical", "active", int(n_und), int(n_contracts), n, f, l, s, "opt1m", "option_intraday")
        n, f, l, s = _span(db, RollingOptionCandle1m, RollingOptionCandle1m.ts, RollingOptionCandle1m.exchange == exch, RollingOptionCandle1m.segment == seg, RollingOptionCandle1m.instrument_type == ins)
        sup = rolling_support(seg, ins or "")
        add(f"rolling:{exch}:{seg}:{ins}", "option_candles_1m_rolling", cat, exch, seg, ins or "", None, "1m", "historical", "rolling", int(n_und), 0, n, f, l, s, "rolling", "rolling")
        if sup is None:
            rows[-1]["blocker"] = f"rolling-options API is not available for {seg}/{ins} (measured)"
        iv = db.scalar(select(func.count()).select_from(RollingOptionCandle1m).where(RollingOptionCandle1m.exchange == exch, RollingOptionCandle1m.segment == seg, RollingOptionCandle1m.instrument_type == ins,
                                                                                  RollingOptionCandle1m.implied_volatility.is_not(None))) or 0
        add(f"rolling_ivoi:{exch}:{seg}:{ins}", "option_iv_oi_rolling", cat, exch, seg, ins or "", None, "1m", "historical", "rolling", int(n_und), 0, int(iv), f, l, s, "rolling", "rolling")
        sn = db.execute(select(func.count(), func.min(OptionChainSnapshot.captured_at), func.max(OptionChainSnapshot.captured_at), func.coalesce(func.sum(OptionChainSnapshot.quotes_stored), 0),
                               func.coalesce(func.sum(OptionChainSnapshot.quotes_with_bid_ask), 0), func.count(func.distinct(OptionChainSnapshot.underlying_symbol))).where(OptionChainSnapshot.exchange == exch)).one()
        add(f"chain:{exch}:{seg}:{ins}", "option_chain_snapshots_live", cat, exch, seg, ins or "", None, "snapshot", "live", "active", int(sn[5]), int(n_contracts), int(sn[0]), _aware(sn[1]), _aware(sn[2]), 0, "chain", "chain_snapshot")
        add(f"ba:{exch}:{seg}:{ins}", "option_bid_ask_historical", cat, exch, seg, ins or "", None, "snapshot", "live", "active", int(sn[5]), int(n_contracts), int(sn[4]), _aware(sn[1]), _aware(sn[2]), 0, "ba", "chain_snapshot")
        rows[-1]["blocker"] = "No historical bid/ask exists at Dhan: rows appear only from live chain snapshots we capture, starting the day capture began."
    db.execute(delete(DerivativeDataCoverage))
    for i in range(0, len(rows), CHUNK):
        db.execute(insert(DerivativeDataCoverage), rows[i:i + CHUNK])
    db.commit()
    return len(rows)


CAPABILITIES_ALIAS = dd.CAPABILITIES


def plan_option_candles_near_atm(db: Session, underlying: str, *, exchange: str, half_width: int = 5, expiry_count: int = 1, lookback_days: int = 10, now: datetime | None = None) -> list[TaskSpec]:
    """Active option 1m candle tasks for strikes within +-half_width listed strikes of the latest captured underlying price (needs a stored chain snapshot; none -> no tasks)."""
    now = now or utcnow()
    snap = db.scalar(select(OptionChainSnapshot).where(OptionChainSnapshot.exchange == exchange, OptionChainSnapshot.underlying_symbol == underlying,
                                                       OptionChainSnapshot.underlying_price.is_not(None)).order_by(OptionChainSnapshot.captured_at.desc()).limit(1))
    if snap is None:
        return []
    cs = [c for c in db.scalars(select(OptionContract).where(OptionContract.exchange == exchange, OptionContract.underlying == underlying, OptionContract.status == "active", OptionContract.valid_to.is_(None)))]
    expiries = sorted({c.expiry for c in cs})[:expiry_count]
    tasks: list[TaskSpec] = []
    for e in expiries:
        strikes = sorted({c.strike for c in cs if c.expiry == e})
        if not strikes:
            continue
        atm = min(range(len(strikes)), key=lambda i: abs(strikes[i] - snap.underlying_price))
        keep = set(strikes[max(0, atm - half_width): atm + half_width + 1])
        for c in cs:
            if c.expiry == e and c.strike in keep and c.segment in dd.CAPABILITIES["options_active_intraday_1m"]["segments"]:
                for a, b in dd.intraday_windows(now - timedelta(days=lookback_days), now):
                    tasks.append(TaskSpec(f"opt1m:{c.id}:{a:%Y%m%d}:{b:%Y%m%d}", "option_intraday", {"contract_id": str(c.id), "start": a.isoformat(), "end": b.isoformat()}))
    return tasks


class RenewingClient:
    """DhanDerivativesClient that re-reads (and renews) the stored Dhan tokens every few minutes, so long jobs survive token expiry. Tokens never leave this object."""

    def __init__(self, session_factory: Callable[[], Session], user_id: UUID, *, refresh_seconds: float = 600.0):
        self.session_factory, self.user_id, self.refresh_seconds = session_factory, user_id, refresh_seconds
        self._client: dd.DhanDerivativesClient | None = None
        self._at = 0.0

    def _get(self) -> dd.DhanDerivativesClient:
        import time
        if self._client is None or time.monotonic() - self._at > self.refresh_seconds:
            from sqlalchemy import select as _select
            from app.api.brokers import _fresh_tokens
            from app.broker_adapters import DhanAdapter
            from app.models import BrokerCredential
            with self.session_factory() as db:
                cred = db.scalar(_select(BrokerCredential).where(BrokerCredential.user_id == self.user_id, BrokerCredential.broker == "dhan"))
                if cred is None:
                    raise BrokerReconnectRequired("Dhan is not connected")
                adapter = DhanAdapter()
                self._client = dd.DhanDerivativesClient(adapter, _fresh_tokens(db, adapter, cred))
            self._at = time.monotonic()
        return self._client

    def __getattr__(self, name: str):
        return getattr(self._get(), name)


def register_futures_instrument(db: Session, contract_id: UUID) -> Any:
    """Create (idempotently) the Instrument row + verified Dhan identifier for one futures contract, so the EXISTING live-feed hub can subscribe to it and the
    strategy/backtest loaders can read its stored candles. No competing subsystem: this only feeds the existing architecture."""
    from app.models import Instrument, InstrumentProviderIdentifier
    c = db.get(DerivativeContract, contract_id)
    if c is None:
        raise ValueError("futures contract not found")
    pid = f"{c.security_id}|{c.segment}"
    ident = db.scalar(select(InstrumentProviderIdentifier).where(InstrumentProviderIdentifier.provider == "dhan", InstrumentProviderIdentifier.provider_instrument_id == pid))
    if ident is not None:
        return ident.instrument
    inst = Instrument(symbol=c.trading_symbol[:64], display_name=(c.display_name or c.trading_symbol)[:160], asset_class="future", venue=c.exchange, currency="INR", country="IN",
                      timezone="Asia/Kolkata", multiplier=c.lot_size or D(1), expiry=c.expiry, created_at=utcnow())
    db.add(inst)
    db.flush()
    db.add(InstrumentProviderIdentifier(instrument_id=inst.id, provider="dhan", provider_instrument_id=pid, provider_symbol=c.trading_symbol[:96], is_verified=True, last_seen_at=utcnow()))
    db.commit()
    return inst


def futures_contract_for_instrument(db: Session, instrument_id: UUID) -> DerivativeContract | None:
    from app.models import InstrumentProviderIdentifier
    ident = db.scalar(select(InstrumentProviderIdentifier).where(InstrumentProviderIdentifier.instrument_id == instrument_id, InstrumentProviderIdentifier.provider == "dhan"))
    if ident is None or "|" not in ident.provider_instrument_id:
        return None
    sid, seg = ident.provider_instrument_id.split("|", 1)
    return db.scalar(select(DerivativeContract).where(DerivativeContract.security_id == sid, DerivativeContract.segment == seg, DerivativeContract.valid_to.is_(None)))


def strategy_underlyings(db: Session) -> list[str]:
    """Underlyings of ACTIVE options strategies (chain-polling priority 1)."""
    from app.models.strategies import Strategy, StrategyVersion
    out: list[str] = []
    for s in db.scalars(select(Strategy).where(Strategy.status == "active")):
        v = db.scalar(select(StrategyVersion).where(StrategyVersion.strategy_id == s.id, StrategyVersion.version == s.current_version))
        u = (((v.definition or {}).get("options") or {}).get("underlying") or {}).get("underlying") if v else None
        if u and u not in out:
            out.append(u)
    return out
