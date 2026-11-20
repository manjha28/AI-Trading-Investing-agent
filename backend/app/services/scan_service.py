"""One strategy's scan pass, shared by manual and scheduled scans. Decision logic stays in ``intraday_runner.scan_instrument`` (unchanged);
signals are created there through ``signal_lifecycle``. This module only drives it and records what was covered."""
from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Instrument
from app.models.jobs import ScanRun
from app.models.strategies import ScanRejection, Strategy, StrategyVersion
from app.services import intraday_runner as runner
from app.services.intraday_strategy import StrategyDefinition


def rejection_counts(db: Session, user_id: UUID, scan_run_id: UUID, strategy_id: UUID | None = None) -> dict[str, int]:
    q = select(ScanRejection.reason_code).where(ScanRejection.user_id == user_id, ScanRejection.scan_run_id == scan_run_id)
    if strategy_id is not None:
        q = q.where(ScanRejection.strategy_id == strategy_id)
    counts: dict[str, int] = {}
    for (code,) in db.execute(q):
        counts[code] = counts.get(code, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def run_strategy_scan(db: Session, *, user_id: UUID, strategy_id: UUID, scan_run_id: UUID, trigger: str, job_id: UUID | None, now: datetime,
                      candle_close: datetime | None = None) -> dict[str, Any]:
    """Scan one strategy's universe. Idempotent per (scan_run_id, strategy): a retried job reuses its ScanRun row."""
    strategy = db.scalar(select(Strategy).where(Strategy.id == strategy_id, Strategy.user_id == user_id))
    if strategy is None or strategy.status != "active":
        raise ValueError("strategy is not active")
    run = db.scalar(select(ScanRun).where(ScanRun.scan_run_id == scan_run_id, ScanRun.strategy_id == strategy_id, ScanRun.user_id == user_id))
    if run is None:
        run = ScanRun(scan_run_id=scan_run_id, user_id=user_id, strategy_id=strategy_id, job_id=job_id, trigger=trigger, status="running", candle_close=candle_close,
                      started_at=now, pairs_scanned=0, instrument_ids=[], new_signals=0, suppressed=0, by_reason={}, errors=[])
        db.add(run)
    run.status, run.job_id = "running", job_id
    db.commit()
    version = db.scalar(select(StrategyVersion).where(StrategyVersion.strategy_id == strategy.id, StrategyVersion.version == strategy.current_version))
    if version is None or version.definition is None:
        raise ValueError("strategy has no intraday definition")
    defn = StrategyDefinition.model_validate(version.definition)
    from app.services.engine_strategies import is_engine
    if is_engine(defn):
        raise ValueError("engine-backed strategies (scalp_hold) are backtest-only and are never scanned")
    errors: list[dict[str, str]] = []
    scanned: list[Instrument] = []
    created = 0
    try:
        instruments = runner.resolve_instruments(db, defn, None, strategy.user_id)
    except runner.RunnerError as error:
        instruments = []
        errors.append({"strategy": strategy.name, "error": str(error)})
    for inst in instruments:
        try:
            rows = runner.scan_instrument(db, strategy, version, defn, inst, now, scan_run_id)
            created += sum(1 for r in rows if r.created_at == now)
            scanned.append(inst)
        except runner.RunnerError as error:
            errors.append({"strategy": strategy.name, "instrument": inst.symbol, "error": str(error)})
    db.commit()
    by_reason = rejection_counts(db, user_id, scan_run_id, strategy_id)
    run.pairs_scanned, run.instrument_ids = len(scanned), [str(i.id) for i in scanned]
    run.new_signals, run.suppressed, run.by_reason = created, sum(by_reason.values()), by_reason
    run.errors, run.status, run.finished_at = errors, "succeeded", datetime.now(now.tzinfo)
    db.commit()
    return {"scan_run_id": str(scan_run_id), "pairs_scanned": run.pairs_scanned, "new_signals": created, "suppressed": run.suppressed, "errors": len(errors)}
