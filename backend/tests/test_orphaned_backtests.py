"""A backtest whose job is gone is closed as failed; one whose job is still queued is left alone. Test data only."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models.jobs import Job
from app.models.strategies import BacktestRun
from app.services import job_queue


def _run(user, created) -> BacktestRun:
    return BacktestRun(id=uuid4(), user_id=user, strategy_id=uuid4(), strategy_version=1, kind="single", status="running", progress=0, config={}, definition_snapshot={},
                       definition_hash="x" * 64, engine_version="t", indicator_version="t", created_at=created)


def test_only_backtests_without_a_live_job_are_closed():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    now = datetime.now(timezone.utc)
    with Session(engine) as db:
        user = uuid4()
        gone, waiting, fresh = _run(user, now - timedelta(days=5)), _run(user, now - timedelta(days=5)), _run(user, now)
        db.add_all([gone, waiting, fresh])
        job_queue.enqueue(db, user_id=user, job_type="backtest", payload={}, idempotency_key=f"backtest:{waiting.id}")
        db.commit()
        assert job_queue.close_orphaned_backtests(db, now=now) == 1
        assert gone.status == "failed" and "Interrupted" in gone.error and gone.finished_at is not None
        assert waiting.status == "running" and fresh.status == "running"
        assert db.query(Job).count() == 1
