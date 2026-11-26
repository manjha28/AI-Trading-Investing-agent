"""Postgres-backed job queue: claim with SELECT ... FOR UPDATE SKIP LOCKED, lease-based stale-lock reclaim, exponential backoff with jitter.

No broker or extra infrastructure. A worker process claims a job (status running, locked_by, locked_at), heartbeats ``locked_at`` while it
works, and finishes it with a compare-and-set on ``locked_by`` so a worker that lost its lease can never overwrite a re-claimed job. At most
one RUNNING job per ``concurrency_key`` is enforced by a partial unique index.
"""
from __future__ import annotations

import os
import random
import socket
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.jobs import Job

LEASE_SECONDS = 60
BACKOFF_BASE_SECONDS = 5.0
BACKOFF_CAP_SECONDS = 300.0
HOST = socket.gethostname()


class PermanentJobError(Exception):
    """Raise from a handler when retrying cannot help (invalid input, deterministic failure)."""


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def new_worker_id() -> str:
    return f"{HOST}:{os.getpid()}:{uuid4().hex[:8]}"


def backoff_seconds(attempt: int, *, rng: random.Random | None = None) -> float:
    """Exponential backoff with +/-25% jitter, capped. ``attempt`` is the number of attempts already made (>= 1)."""
    base = min(BACKOFF_CAP_SECONDS, BACKOFF_BASE_SECONDS * (2 ** max(0, attempt - 1)))
    return base * (1 + (rng or random).uniform(-0.25, 0.25))


def enqueue(db: Session, *, user_id: UUID, job_type: str, payload: dict[str, Any], idempotency_key: str | None = None, concurrency_key: str | None = None,
            max_attempts: int = 3, run_at: datetime | None = None) -> tuple[Job, bool]:
    """Queue a job. With an idempotency key the same logical job is never queued twice: the existing row is returned and ``created`` is False."""
    if idempotency_key is not None:
        existing = db.scalar(select(Job).where(Job.user_id == user_id, Job.idempotency_key == idempotency_key))
        if existing is not None:
            return existing, False
    now = now_utc()
    job = Job(user_id=user_id, job_type=job_type, payload=payload, status="queued", attempts=0, max_attempts=max_attempts, next_attempt_at=run_at or now,
              progress={"pct": 0, "stage": "queued"}, idempotency_key=idempotency_key, concurrency_key=concurrency_key, created_at=now)
    db.add(job)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.scalar(select(Job).where(Job.user_id == user_id, Job.idempotency_key == idempotency_key)) if idempotency_key is not None else None
        if existing is None:
            raise
        return existing, False
    return job, True


def claim_next(db: Session, worker_id: str, *, now: datetime | None = None, job_types: tuple[str, ...] | None = None) -> Job | None:
    """Atomically claim one runnable job, or None. Safe under concurrent workers: rows locked by another claimer are skipped, and the
    partial unique index rejects a second RUNNING job for the same concurrency key (that candidate is skipped, not failed)."""
    now = now or now_utc()
    skipped: set[UUID] = set()
    for _ in range(25):
        q = (select(Job).where(Job.status == "queued", Job.next_attempt_at <= now).order_by(Job.next_attempt_at, Job.created_at).limit(1)
             .with_for_update(skip_locked=True))
        if job_types:
            q = q.where(Job.job_type.in_(job_types))
        if skipped:
            q = q.where(Job.id.not_in(skipped))
        job = db.scalar(q)
        if job is None:
            db.rollback()
            return None
        jid = job.id
        job.status, job.locked_by, job.locked_at = "running", worker_id, now
        job.attempts += 1
        job.started_at = job.started_at or now
        job.error = None if job.attempts == 1 else job.error
        try:
            db.commit()
            return job
        except IntegrityError:          # another job with this concurrency_key is already running
            db.rollback()
            skipped.add(jid)
    return None


def heartbeat(db: Session, job_id: UUID, worker_id: str, *, progress: dict[str, Any] | None = None) -> bool:
    """Extend the lease (and optionally publish progress). False means this worker no longer owns the job."""
    values: dict[str, Any] = {"locked_at": now_utc()}
    if progress is not None:
        values["progress"] = progress
    res = db.execute(update(Job).where(Job.id == job_id, Job.locked_by == worker_id, Job.status == "running").values(**values))
    db.commit()
    return res.rowcount == 1


def succeed(db: Session, job_id: UUID, worker_id: str, result: dict[str, Any] | None) -> bool:
    res = db.execute(update(Job).where(Job.id == job_id, Job.locked_by == worker_id, Job.status == "running")
                     .values(status="succeeded", result=result or {}, error=None, finished_at=now_utc(), locked_by=None, locked_at=None, progress={"pct": 100, "stage": "done"}))
    db.commit()
    return res.rowcount == 1


def fail(db: Session, job_id: UUID, worker_id: str, error: str, *, retryable: bool = True) -> str | None:
    """Record a failure. Returns the new status ('queued' for a retry with backoff, 'failed' when out of attempts or not retryable), or None if the lease was lost."""
    job = db.scalar(select(Job).where(Job.id == job_id, Job.locked_by == worker_id, Job.status == "running").with_for_update())
    if job is None:
        db.rollback()
        return None
    now = now_utc()
    job.error = error[:2000]
    job.locked_by = job.locked_at = None
    if retryable and job.attempts < job.max_attempts:
        job.status, job.next_attempt_at = "queued", now + timedelta(seconds=backoff_seconds(job.attempts))
        job.progress = {"pct": 0, "stage": "retry_scheduled", "attempt": job.attempts, "max_attempts": job.max_attempts}
    else:
        job.status, job.finished_at = "failed", now
    status = job.status
    db.commit()
    return status


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def reclaim_stale(db: Session, *, now: datetime | None = None, lease_seconds: int = LEASE_SECONDS) -> list[UUID]:
    """Make jobs of a dead worker runnable again: lease expired, or locked by a process on THIS host that no longer exists.
    A job that has used all its attempts is failed with a reason instead of being re-queued."""
    now = now or now_utc()
    reclaimed: list[UUID] = []
    for job in db.scalars(select(Job).where(Job.status == "running").with_for_update(skip_locked=True)):
        owner_host, _, rest = (job.locked_by or "").partition(":")
        pid = rest.partition(":")[0]
        expired = job.locked_at is None or (job.locked_at if job.locked_at.tzinfo else job.locked_at.replace(tzinfo=timezone.utc)) < now - timedelta(seconds=lease_seconds)
        orphaned = owner_host == HOST and pid.isdigit() and not _pid_alive(int(pid))
        if not (expired or orphaned):
            continue
        why = "its worker process no longer exists" if orphaned else f"its worker stopped heartbeating for over {lease_seconds} s"
        job.locked_by = job.locked_at = None
        if job.attempts >= job.max_attempts:
            job.status, job.finished_at, job.error = "failed", now, f"Worker lost while running ({why}); no attempts left."
        else:
            job.status, job.next_attempt_at, job.error = "queued", now, f"Worker lost while running ({why}); re-queued."
            job.progress = {"pct": 0, "stage": "requeued_after_worker_loss", "attempt": job.attempts, "max_attempts": job.max_attempts}
        reclaimed.append(job.id)
    db.commit()
    return reclaimed


def close_orphaned_backtests(db: Session, *, now: datetime | None = None, older_than: timedelta = timedelta(minutes=10)) -> int:
    """A backtest left "queued"/"running" although its job is finished or gone (the server restarted under it) is marked failed, with the reason."""
    from app.models.strategies import BacktestRun

    now = now or now_utc()
    closed = 0
    for run in db.scalars(select(BacktestRun).where(BacktestRun.status.in_(("queued", "running")), BacktestRun.created_at < now - older_than)):
        job = db.scalar(select(Job).where(Job.user_id == run.user_id, Job.idempotency_key == f"backtest:{run.id}"))
        if job is not None and job.status in ("queued", "running"):
            continue
        run.status, run.finished_at = "failed", now
        run.error = (job.error if job is not None and job.error else None) or "Interrupted: the background job ended without finishing this backtest. Run it again."
        closed += 1
    if closed:
        db.commit()
    return closed


def retry(db: Session, job: Job) -> Job:
    """Owner action: put a failed or cancelled job back in the queue with a fresh attempt budget."""
    if job.status not in ("failed", "cancelled"):
        raise ValueError(f"only failed or cancelled jobs can be retried (this one is {job.status})")
    job.status, job.attempts, job.error, job.finished_at, job.next_attempt_at = "queued", 0, None, None, now_utc()
    job.progress = {"pct": 0, "stage": "queued"}
    db.commit()
    return job


def cancel(db: Session, job: Job) -> Job:
    if job.status != "queued":
        raise ValueError(f"only queued jobs can be cancelled (this one is {job.status})")
    job.status, job.finished_at = "cancelled", now_utc()
    db.commit()
    return job
