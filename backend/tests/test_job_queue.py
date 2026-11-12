"""Durable job queue, scanner scheduling and restart recovery.

Claim-safety and restart tests run against a REAL Postgres (a throwaway database created and dropped by the fixture) because SKIP LOCKED and
the partial unique index are Postgres behaviour. If Postgres is unreachable they are SKIPPED with that reason, never passed vacuously.
"""
import os
import threading
import uuid
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.db import Base
from app.models import User
from app.models.jobs import Job, ScannerSchedule, ScannerSettings
from app.services import job_queue, scan_scheduler

IST = ZoneInfo("Asia/Kolkata")


def _new_user(db: Session) -> uuid.UUID:
    u = User(email=f"{uuid.uuid4().hex[:8]}@example.com", password_hash="x", created_at=datetime.now(timezone.utc))
    db.add(u)
    db.commit()
    return u.id


@pytest.fixture
def sqlite_db():
    e = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(e)
    with Session(e) as db:
        yield db


@pytest.fixture
def pg_engine():
    admin = create_engine(settings.database_url, isolation_level="AUTOCOMMIT")
    name = f"jobtest_{uuid.uuid4().hex[:10]}"
    try:
        with admin.connect() as c:
            c.execute(text(f'create database "{name}"'))
    except Exception as error:  # noqa: BLE001
        pytest.skip(f"Postgres is not reachable ({type(error).__name__}); claim-safety tests need a real Postgres")
    url = make_url(settings.database_url).set(database=name)
    engine = create_engine(url, pool_size=10)
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()
    with admin.connect() as c:
        c.execute(text(f'drop database "{name}" with (force)'))


# ───────────── queue semantics (any database) ─────────────
def test_idempotency_key_never_queues_the_same_logical_job_twice(sqlite_db):
    uid = _new_user(sqlite_db)
    a, created_a = job_queue.enqueue(sqlite_db, user_id=uid, job_type="scan", payload={}, idempotency_key="scan:s1:2026-11-12T04:00:00+00:00")
    b, created_b = job_queue.enqueue(sqlite_db, user_id=uid, job_type="scan", payload={}, idempotency_key="scan:s1:2026-11-12T04:00:00+00:00")
    assert created_a and not created_b and a.id == b.id
    assert sqlite_db.scalar(text("select count(*) from jobs")) == 1


def test_failure_retries_with_exponential_backoff_then_fails_at_the_attempt_cap(sqlite_db):
    uid = _new_user(sqlite_db)
    job, _ = job_queue.enqueue(sqlite_db, user_id=uid, job_type="backtest", payload={}, max_attempts=3)
    delays = []
    for attempt in (1, 2, 3):
        claimed = job_queue.claim_next(sqlite_db, "w1", now=datetime.now(timezone.utc) + timedelta(days=attempt))
        assert claimed is not None and claimed.attempts == attempt
        status = job_queue.fail(sqlite_db, job.id, "w1", f"boom {attempt}")
        sqlite_db.refresh(job)
        if status == "queued":
            delays.append((job.next_attempt_at.replace(tzinfo=timezone.utc) - datetime.now(timezone.utc)).total_seconds())
    assert status == "failed" and job.status == "failed" and job.error == "boom 3" and job.finished_at is not None
    assert delays[1] > delays[0] * 1.2                                    # roughly doubles (jitter is +/-25%)
    assert job_queue.backoff_seconds(1, rng=__import__("random").Random(1)) < job_queue.backoff_seconds(4, rng=__import__("random").Random(1)) <= job_queue.BACKOFF_CAP_SECONDS * 1.25


def test_permanent_failure_does_not_retry_and_a_lost_lease_cannot_overwrite(sqlite_db):
    uid = _new_user(sqlite_db)
    job, _ = job_queue.enqueue(sqlite_db, user_id=uid, job_type="backtest", payload={}, max_attempts=5)
    job_queue.claim_next(sqlite_db, "w1")
    assert job_queue.fail(sqlite_db, job.id, "w1", "bad input", retryable=False) == "failed"
    job2, _ = job_queue.enqueue(sqlite_db, user_id=uid, job_type="backtest", payload={})
    job_queue.claim_next(sqlite_db, "w1")
    assert job_queue.succeed(sqlite_db, job2.id, "someone-else", {}) is False      # not the lock holder
    assert job_queue.heartbeat(sqlite_db, job2.id, "someone-else") is False


def test_one_running_job_per_concurrency_key(sqlite_db):
    uid = _new_user(sqlite_db)
    a, _ = job_queue.enqueue(sqlite_db, user_id=uid, job_type="scan", payload={}, concurrency_key="scan:s1")
    b, _ = job_queue.enqueue(sqlite_db, user_id=uid, job_type="scan", payload={}, concurrency_key="scan:s1")
    first = job_queue.claim_next(sqlite_db, "w1")
    assert first.id == a.id
    assert job_queue.claim_next(sqlite_db, "w2") is None                   # b waits: the same strategy is already being scanned
    job_queue.succeed(sqlite_db, a.id, "w1", {})
    assert job_queue.claim_next(sqlite_db, "w2").id == b.id


def test_stale_lease_requeues_and_exhausted_jobs_fail_with_a_reason(sqlite_db):
    uid = _new_user(sqlite_db)
    ok, _ = job_queue.enqueue(sqlite_db, user_id=uid, job_type="backtest", payload={}, max_attempts=3)
    spent, _ = job_queue.enqueue(sqlite_db, user_id=uid, job_type="backtest", payload={}, max_attempts=1)
    job_queue.claim_next(sqlite_db, "other-host:1:x")
    job_queue.claim_next(sqlite_db, "other-host:2:y")
    later = datetime.now(timezone.utc) + timedelta(seconds=job_queue.LEASE_SECONDS + 5)
    assert len(job_queue.reclaim_stale(sqlite_db, now=later)) == 2
    sqlite_db.refresh(ok); sqlite_db.refresh(spent)
    assert ok.status == "queued" and "re-queued" in ok.error
    assert spent.status == "failed" and "no attempts left" in spent.error


# ───────────── real Postgres ─────────────
def test_two_workers_never_claim_the_same_job(pg_engine):
    with Session(pg_engine) as db:
        uid = _new_user(db)
        for i in range(40):
            job_queue.enqueue(db, user_id=uid, job_type="scan", payload={"i": i})
    claimed: list[list[uuid.UUID]] = [[], [], [], []]
    barrier = threading.Barrier(4)

    def work(slot: int) -> None:
        barrier.wait()
        with Session(pg_engine) as db:
            while (job := job_queue.claim_next(db, f"w{slot}")) is not None:
                claimed[slot].append(job.id)

    threads = [threading.Thread(target=work, args=(n,)) for n in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    everything = [j for slot in claimed for j in slot]
    assert len(everything) == 40 and len(set(everything)) == 40            # every job claimed exactly once
    assert sum(1 for slot in claimed if slot) >= 2                          # and the work really was shared between workers
    with Session(pg_engine) as db:
        assert db.scalar(select(Job.attempts).where(Job.id == everything[0])) == 1


def test_postgres_forbids_two_running_jobs_for_one_strategy_even_under_a_race(pg_engine):
    with Session(pg_engine) as db:
        uid = _new_user(db)
        for _ in range(6):
            job_queue.enqueue(db, user_id=uid, job_type="scan", payload={}, concurrency_key="scan:same")
    got: list[uuid.UUID] = []
    barrier = threading.Barrier(3)

    def work(n: int) -> None:
        barrier.wait()
        with Session(pg_engine) as db:
            job = job_queue.claim_next(db, f"w{n}")
            if job is not None:
                got.append(job.id)

    threads = [threading.Thread(target=work, args=(n,)) for n in range(3)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(got) == 1
    with Session(pg_engine) as db:
        assert db.scalar(text("select count(*) from jobs where status='running'")) == 1


def test_a_job_running_when_the_process_died_becomes_runnable_again_and_completes(pg_engine):
    """Restart recovery: worker 'A' claims a job and its process disappears (its pid is not alive). The next worker's reclaim re-queues it
    at once (no waiting for the lease) and a new worker runs it to completion."""
    dead_pid = 4_000_000                                                   # above the kernel pid limit on Linux: certainly not alive
    with Session(pg_engine) as db:
        uid = _new_user(db)
        job, _ = job_queue.enqueue(db, user_id=uid, job_type="backtest", payload={"run_id": "r"}, max_attempts=3)
        job_queue.claim_next(db, f"{job_queue.HOST}:{dead_pid}:aaaa")
        db.refresh(job)
        assert job.status == "running"
    with Session(pg_engine) as db:                                         # "after restart"
        assert job_queue.reclaim_stale(db) == [job.id]
    with Session(pg_engine) as db:
        again = job_queue.claim_next(db, "restarted-worker")
        assert again is not None and again.id == job.id and again.attempts == 2
        assert job_queue.succeed(db, job.id, "restarted-worker", {"status": "completed"})
        done = db.get(Job, job.id)
        assert done.status == "succeeded" and "worker process no longer exists" not in (done.error or "")


# ───────────── scheduling ─────────────
def _ist(y, mo, d, h, mi, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=IST).astimezone(timezone.utc)


def test_scans_align_to_candle_close_plus_delay_not_an_arbitrary_timer():
    nxt = scan_scheduler.next_scan_time(_ist(2026, 11, 12, 10, 3), 5, "equity_in")      # Thursday, session open
    assert nxt == _ist(2026, 11, 12, 10, 5, scan_scheduler.CANDLE_CLOSE_DELAY_SECONDS)
    assert scan_scheduler.next_scan_time(nxt, 5, "equity_in") == _ist(2026, 11, 12, 10, 10, scan_scheduler.CANDLE_CLOSE_DELAY_SECONDS)
    assert scan_scheduler.next_scan_time(_ist(2026, 11, 12, 10, 3), 15, "equity_in") == _ist(2026, 11, 12, 10, 15, scan_scheduler.CANDLE_CLOSE_DELAY_SECONDS)


def test_no_scan_outside_session_hours_or_on_non_trading_days():
    after_close = scan_scheduler.next_scan_time(_ist(2026, 11, 12, 15, 40), 5, "equity_in")
    assert after_close == _ist(2026, 11, 13, 9, 20, scan_scheduler.CANDLE_CLOSE_DELAY_SECONDS)   # next session's first 5m close, never the evening
    assert scan_scheduler.next_scan_time(_ist(2026, 11, 12, 8, 0), 5, "equity_in") == _ist(2026, 11, 12, 9, 20, scan_scheduler.CANDLE_CLOSE_DELAY_SECONDS)
    saturday = scan_scheduler.next_scan_time(_ist(2026, 11, 13, 16, 0), 5, "equity_in")           # Friday evening -> Monday 16 Nov
    assert saturday.astimezone(IST).date() == date(2026, 11, 16)
    holiday_eve = scan_scheduler.next_scan_time(_ist(2026, 11, 9, 16, 0), 5, "equity_in")         # 10 Nov 2026 is an NSE holiday in the configured calendar
    assert holiday_eve.astimezone(IST).date() == date(2026, 11, 11)
    assert scan_scheduler.candle_closes_on(date(2026, 11, 8), 5, "equity_in") == []                # Sunday
    assert scan_scheduler.candle_closes_on(date(2031, 11, 12), 5, "equity_in") == []               # year not covered by the calendar: fail closed


def _strategy(db: Session, uid: uuid.UUID) -> uuid.UUID:
    from app.models.strategies import Strategy, StrategyVersion
    from app.services.starter_strategies import STARTERS

    d = dict(STARTERS[0])
    from app.models import Instrument

    inst = Instrument(symbol="SCHED", display_name="Sched", asset_class="equity", venue="NSE", currency="INR", country="IN", timezone="Asia/Kolkata", is_synthetic=False,
                      relink_required=False, created_at=datetime.now(timezone.utc))
    db.add(inst)
    db.flush()
    st = Strategy(user_id=uid, instrument_id=inst.id, name="sched", status="active", current_version=1, created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc))
    db.add(st)
    db.flush()
    db.add(StrategyVersion(strategy_id=st.id, version=1, params={}, definition=d, created_at=datetime.now(timezone.utc)))
    db.commit()
    return st.id


def test_tick_enqueues_once_per_candle_respects_the_kill_switch_and_never_overlaps(sqlite_db):
    uid = _new_user(sqlite_db)
    sid = _strategy(sqlite_db, uid)
    now = _ist(2026, 11, 12, 10, 5, 20)
    due = _ist(2026, 11, 12, 10, 5, scan_scheduler.CANDLE_CLOSE_DELAY_SECONDS)
    sqlite_db.add(ScannerSchedule(user_id=uid, strategy_id=sid, enabled=True, cadence_minutes=5, next_run_at=due, updated_at=now))
    sqlite_db.commit()
    assert scan_scheduler.tick(sqlite_db, now)["enqueued"] == 0                       # master switch is off by default: nothing is scheduled
    sqlite_db.add(ScannerSettings(user_id=uid, scans_enabled=True, updated_at=now))
    sqlite_db.commit()
    assert scan_scheduler.tick(sqlite_db, now)["enqueued"] == 1
    job = sqlite_db.scalar(select(Job))
    assert job.concurrency_key == f"scan:{sid}" and job.payload["trigger"] == "schedule" and job.payload["candle_close"] == _ist(2026, 11, 12, 10, 5).isoformat()
    sched = sqlite_db.scalar(select(ScannerSchedule))
    assert sched.next_run_at.replace(tzinfo=timezone.utc) == _ist(2026, 11, 12, 10, 10, scan_scheduler.CANDLE_CLOSE_DELAY_SECONDS)
    # next candle arrives while the previous scan is still queued/running: skipped, not overlapped
    later = _ist(2026, 11, 12, 10, 10, 20)
    assert scan_scheduler.tick(sqlite_db, later) == {"enqueued": 0, "skipped_overlap": 1, "skipped_missed": 0, "rescheduled": 0}
    assert sqlite_db.scalar(text("select count(*) from jobs")) == 1
    # a process that was down for an hour does not run stale boundaries late
    sqlite_db.execute(text("update jobs set status='succeeded'"))
    sqlite_db.commit()
    assert scan_scheduler.tick(sqlite_db, _ist(2026, 11, 12, 11, 30))["skipped_missed"] == 1


def test_scan_cycle_request_estimate_reports_zero_provider_requests(sqlite_db):
    uid = _new_user(sqlite_db)
    est = scan_scheduler.request_estimate(sqlite_db, uid)
    assert est["provider_requests_per_scan_cycle"] == 0 and "no provider HTTP call" in est["basis"]


def test_the_browser_timer_is_gone_and_only_the_server_schedules():
    root = os.path.join(os.path.dirname(__file__), "..", "..", "frontend", "src", "signals", "LiveSignals.tsx")
    source = open(root).read()
    assert "setInterval" not in source and "Auto-scan" not in source
