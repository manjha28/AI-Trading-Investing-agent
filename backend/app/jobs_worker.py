"""In-process job worker + scan scheduler, started from the FastAPI lifespan like the other asyncio loops. No extra infrastructure."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError

from app.db import Session, engine
from app.models.jobs import Job
from app.services import job_queue, scan_scheduler
from app.services.job_handlers import HANDLERS, JobContext, mark_scan_failed

logger = logging.getLogger(__name__)
POLL_SECONDS = 2.0
HEARTBEAT_SECONDS = 15.0
SCHEDULER_SECONDS = 15.0
RECLAIM_SECONDS = 30.0
MAX_CONCURRENT_JOBS = 3


def _run_handler(job: ClaimedJob, worker_id: str) -> tuple[str, str | None, bool]:
    """Executed in a thread. Returns (outcome, error, retryable)."""
    handler = HANDLERS.get(job.job_type)
    if handler is None:
        return "fail", f"no handler for job type {job.job_type!r}", False

    def progress(**values) -> None:
        with Session(engine) as db:
            job_queue.heartbeat(db, job.id, worker_id, progress=values)

    ctx = JobContext(job_id=job.id, user_id=job.user_id, payload=dict(job.payload), attempt=job.attempts, progress=progress)
    try:
        result = handler(ctx)
    except job_queue.PermanentJobError as error:
        return "fail", str(error), False
    except Exception as error:  # noqa: BLE001 - every failure is recorded on the job with its class and message
        logger.exception("job_failed", extra={"job_id": str(job.id), "job_type": job.job_type})
        return "fail", f"{type(error).__name__}: {error}", True
    with Session(engine) as db:
        if not job_queue.succeed(db, job.id, worker_id, result):
            logger.warning("job_result_discarded_lease_lost job=%s", job.id)
    return "ok", None, False


async def _execute(job: ClaimedJob, worker_id: str) -> None:
    async def beat() -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            await asyncio.to_thread(_heartbeat_only, job.id, worker_id)

    beater = asyncio.create_task(beat())
    try:
        outcome, error, retryable = await asyncio.to_thread(_run_handler, job, worker_id)
    except Exception as exc:  # noqa: BLE001 - never leave a claimed job running because the runner itself failed
        logger.exception("job_runner_failed", extra={"job_id": str(job.id)})
        outcome, error, retryable = "fail", f"worker error: {type(exc).__name__}: {exc}", True
    finally:
        beater.cancel()
    if outcome == "fail":
        await asyncio.to_thread(_record_failure, job.id, worker_id, error or "failed", retryable)


def _heartbeat_only(job_id, worker_id) -> None:
    with Session(engine) as db:
        job_queue.heartbeat(db, job_id, worker_id)


def _record_failure(job_id, worker_id, error: str, retryable: bool) -> None:
    with Session(engine) as db:
        status = job_queue.fail(db, job_id, worker_id, error, retryable=retryable)
        if status == "failed":
            job = db.get(Job, job_id)
            if job is not None and job.job_type == "scan":
                mark_scan_failed(db, job)


@dataclass(frozen=True)
class ClaimedJob:
    """Plain snapshot of a claimed job, safe to use after its database session has closed."""
    id: UUID
    user_id: UUID
    job_type: str
    payload: dict
    attempts: int


def _claim(worker_id: str) -> ClaimedJob | None:
    with Session(engine) as db:
        job = job_queue.claim_next(db, worker_id)
        if job is None:
            return None
        return ClaimedJob(id=job.id, user_id=job.user_id, job_type=job.job_type, payload=dict(job.payload), attempts=job.attempts)


def _housekeeping(do_scheduler: bool, do_reclaim: bool) -> None:
    with Session(engine) as db:
        if do_reclaim:
            moved = job_queue.reclaim_stale(db)
            if moved:
                logger.warning("jobs_reclaimed count=%s", len(moved))
        if do_scheduler:
            scan_scheduler.tick(db)


async def job_worker_loop() -> None:
    worker_id = job_queue.new_worker_id()
    logger.info("job_worker_started worker=%s", worker_id)
    running: set[asyncio.Task] = set()
    since_sched = since_reclaim = 1e9      # run both immediately on start: a job orphaned by the previous process becomes runnable at once
    while True:
        try:
            since_sched += POLL_SECONDS
            since_reclaim += POLL_SECONDS
            do_reclaim, do_sched = since_reclaim >= RECLAIM_SECONDS, since_sched >= SCHEDULER_SECONDS
            if do_reclaim or do_sched:
                await asyncio.to_thread(_housekeeping, do_sched, do_reclaim)
                since_reclaim = 0 if do_reclaim else since_reclaim
                since_sched = 0 if do_sched else since_sched
            running = {t for t in running if not t.done()}
            while len(running) < MAX_CONCURRENT_JOBS:
                job = await asyncio.to_thread(_claim, worker_id)
                if job is None:
                    break
                running.add(asyncio.create_task(_execute(job, worker_id)))
        except asyncio.CancelledError:
            for task in running:
                task.cancel()
            raise
        except SQLAlchemyError:
            logger.exception("job worker database error")
        await asyncio.sleep(POLL_SECONDS)
