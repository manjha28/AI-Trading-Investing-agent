"""Handlers for queued jobs. Each receives a ``JobContext`` and returns a small JSON result; raise ``PermanentJobError`` when a retry cannot help."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.jobs import Job
from app.services.job_queue import PermanentJobError, now_utc


@dataclass
class JobContext:
    job_id: UUID
    user_id: UUID
    payload: dict[str, Any]
    attempt: int
    progress: Callable[..., None]


def handle_backtest(ctx: JobContext) -> dict[str, Any]:
    """Run a stored BacktestRun. Safe to re-run after a crash: the run is deterministic and already-completed runs are not repeated."""
    from app.api import signals_lab
    from app.db import Session as SessionFactory, engine
    from app.models.strategies import BacktestRun

    run_id = UUID(ctx.payload["run_id"])
    with SessionFactory(engine) as db:
        run = db.get(BacktestRun, run_id)
        if run is None:
            raise PermanentJobError("backtest run no longer exists")
        if run.status == "completed":
            return {"run_id": str(run_id), "status": "completed", "note": "already completed before this attempt"}
    ctx.progress(pct=5, stage="running_backtest", run_id=str(run_id))
    signals_lab._run_in_background(engine, run_id, [UUID(x) for x in ctx.payload["instrument_ids"]])
    with SessionFactory(engine) as db:
        run = db.get(BacktestRun, run_id)
        if run is None or run.status != "completed":
            raise PermanentJobError((run.error if run else None) or "backtest did not complete")
    return {"run_id": str(run_id), "status": "completed"}


def handle_scan(ctx: JobContext) -> dict[str, Any]:
    from app.db import Session as SessionFactory, engine
    from app.services.scan_service import run_strategy_scan
    from datetime import datetime

    payload = ctx.payload
    candle_close = datetime.fromisoformat(payload["candle_close"]) if payload.get("candle_close") else None
    ctx.progress(pct=10, stage="scanning", strategy_id=payload["strategy_id"])
    with SessionFactory(engine) as db:
        try:
            return run_strategy_scan(db, user_id=ctx.user_id, strategy_id=UUID(payload["strategy_id"]), scan_run_id=UUID(payload["scan_run_id"]),
                                     trigger=payload.get("trigger", "manual"), job_id=ctx.job_id, now=now_utc(), candle_close=candle_close)
        except ValueError as error:
            raise PermanentJobError(str(error)) from None


def handle_var_backtest(ctx: JobContext) -> dict[str, Any]:
    """Run a stored VAR (hypothetical) backtest from stored candles. Deterministic; a finished run is not repeated."""
    from datetime import datetime, timezone

    from app.db import Session as SessionFactory, engine
    from app.models.var_strategy import VarBacktestRun
    from app.services.var import service

    run_id = UUID(ctx.payload["run_id"])
    with SessionFactory(engine) as db:
        run = db.get(VarBacktestRun, run_id)
        if run is None:
            raise PermanentJobError("VAR backtest run no longer exists")
        if run.status in ("completed", "unavailable", "no_usable_data", "order_flow_unavailable"):
            return {"run_id": str(run_id), "status": run.status, "note": "already finished"}
        run.status = "running"
        db.commit()
        request, config = dict(run.request), dict(run.config_snapshot)
    ctx.progress(pct=10, stage="running_var_backtest", run_id=str(run_id))
    try:
        with SessionFactory(engine) as db:
            result = service.jsonable(service.run_stored_backtest(db, request, config))
    except Exception as error:   # recorded on the run, then surfaced; never swallowed
        with SessionFactory(engine) as db:
            run = db.get(VarBacktestRun, run_id)
            run.status, run.error, run.finished_at = "failed", f"{type(error).__name__}: {error}"[:2000], datetime.now(timezone.utc)
            db.commit()
        raise PermanentJobError(f"{type(error).__name__}: {error}") from None
    with SessionFactory(engine) as db:
        run = db.get(VarBacktestRun, run_id)
        run.status = result.get("status") if result.get("status") in ("unavailable", "no_usable_data", "order_flow_unavailable") else "completed"
        run.result, run.finished_at = result, datetime.now(timezone.utc)
        db.commit()
    return {"run_id": str(run_id), "status": run.status}


HANDLERS: dict[str, Callable[[JobContext], dict[str, Any]]] = {"backtest": handle_backtest, "scan": handle_scan, "var_backtest": handle_var_backtest}


def mark_scan_failed(db: Session, job: Job) -> None:
    """Keep scan history honest when a scan job ends in failure."""
    from sqlalchemy import select

    from app.models.jobs import ScanRun

    run = db.scalar(select(ScanRun).where(ScanRun.job_id == job.id))
    if run is not None and run.status != "succeeded":
        run.status, run.finished_at = "failed", now_utc()
        run.errors = [*run.errors, {"error": (job.error or "scan failed")[:500]}]
        db.commit()
