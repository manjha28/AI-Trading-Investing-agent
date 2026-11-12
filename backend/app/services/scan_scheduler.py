"""Candle-close-aligned scan scheduling (blueprint §6.1). Pure time functions plus one tick that enqueues due scans into the job queue.

A scan is due shortly AFTER a decision-timeframe candle closes (``CANDLE_CLOSE_DELAY_SECONDS``), never on an arbitrary timer. Equity schedules
follow the NSE session (09:15-15:30 IST, trading days per ``trading_calendar``; a year the calendar does not cover is treated as closed:
fail closed). A scan whose boundary was missed (process down) is skipped, not run late.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.jobs import Job, ScannerSchedule, ScannerSettings
from app.models.strategies import Strategy, StrategyVersion
from app.services import job_queue
from app.services.intraday_strategy import StrategyDefinition
from app.services.timeframes import EQUITY_IN, TIMEFRAME_MINUTES
from app.services.trading_calendar import TRADING, classify_day

CANDLE_CLOSE_DELAY_SECONDS = 15
MISSED_GRACE_MINUTES = 5
MIN_CADENCE_MINUTES = 1
IST = ZoneInfo("Asia/Kolkata")
SCAN_MAX_ATTEMPTS = 2


def candle_closes_on(day, cadence: int, market_key: str) -> list[datetime]:
    """UTC instants at which a candle of ``cadence`` minutes closes on a local session day (equity), in order."""
    if market_key != "equity_in":
        raise ValueError("only equity sessions have a finite list of closes")
    if classify_day(day, market_key) != TRADING:
        return []
    open_local = datetime.combine(day, EQUITY_IN.session_open, tzinfo=IST)
    n = EQUITY_IN.session_minutes // cadence
    return [(open_local + timedelta(minutes=cadence * k)).astimezone(timezone.utc) for k in range(1, n + 1)]


def next_scan_time(after: datetime, cadence: int, market_key: str) -> datetime | None:
    """First scan instant (candle close + delay) strictly after ``after``, or None when none exists within ~two weeks."""
    delay = timedelta(seconds=CANDLE_CLOSE_DELAY_SECONDS)
    if market_key == "crypto":
        epoch_s = int(after.timestamp()) - CANDLE_CLOSE_DELAY_SECONDS
        step = cadence * 60
        close = datetime.fromtimestamp((epoch_s // step + 1) * step, timezone.utc)
        return close + delay
    day = after.astimezone(IST).date()
    for offset in range(0, 15):
        for close in candle_closes_on(day + timedelta(days=offset), cadence, market_key):
            if close + delay > after:
                return close + delay
    return None


def boundary_for(due: datetime) -> datetime:
    """The candle close a due scan instant belongs to."""
    return due - timedelta(seconds=CANDLE_CLOSE_DELAY_SECONDS)


def default_cadence(defn: StrategyDefinition) -> int:
    return TIMEFRAME_MINUTES[defn.decision_timeframe]


def valid_cadence(defn: StrategyDefinition, cadence: int) -> bool:
    base = default_cadence(defn)
    return cadence >= base and cadence % base == 0


def market_key_of(defn: StrategyDefinition) -> str:
    return defn.market


def tick(db: Session, now: datetime | None = None) -> dict[str, int]:
    """Enqueue every due scan. Safe to call repeatedly: enqueueing is idempotent per (strategy, candle close) and overlap-guarded."""
    now = now or job_queue.now_utc()
    stats = {"enqueued": 0, "skipped_overlap": 0, "skipped_missed": 0, "rescheduled": 0}
    owners = [r.user_id for r in db.scalars(select(ScannerSettings).where(ScannerSettings.scans_enabled.is_(True)))]
    if not owners:
        return stats
    schedules = list(db.scalars(select(ScannerSchedule).where(ScannerSchedule.user_id.in_(owners), ScannerSchedule.enabled.is_(True))))
    for sched in schedules:
        strategy = db.get(Strategy, sched.strategy_id)
        version = db.scalar(select(StrategyVersion).where(StrategyVersion.strategy_id == sched.strategy_id, StrategyVersion.version == (strategy.current_version if strategy else 0)))
        if strategy is None or strategy.status != "active" or version is None or version.definition is None:
            sched.last_skip_reason, sched.next_run_at = "strategy_not_active", None
            continue
        defn = StrategyDefinition.model_validate(version.definition)
        market = market_key_of(defn)
        if sched.next_run_at is None:
            sched.next_run_at = next_scan_time(now, sched.cadence_minutes, market)
            stats["rescheduled"] += 1
            continue
        due = sched.next_run_at if sched.next_run_at.tzinfo else sched.next_run_at.replace(tzinfo=timezone.utc)
        if due > now:
            continue
        sched.next_run_at = next_scan_time(now, sched.cadence_minutes, market)
        if now - due > timedelta(minutes=max(MISSED_GRACE_MINUTES, 1)):
            sched.last_skip_reason = f"missed_boundary_{boundary_for(due).isoformat()}"
            stats["skipped_missed"] += 1
            continue
        key = f"scan:{sched.strategy_id}"
        active = db.scalar(select(Job.id).where(Job.user_id == sched.user_id, Job.concurrency_key == key, Job.status.in_(("queued", "running"))).limit(1))
        if active is not None:
            sched.last_skip_reason = "previous_scan_still_in_flight"
            stats["skipped_overlap"] += 1
            continue
        close = boundary_for(due)
        job, created = job_queue.enqueue(db, user_id=sched.user_id, job_type="scan", idempotency_key=f"scan:{sched.strategy_id}:{close.isoformat()}", concurrency_key=key,
                                         max_attempts=SCAN_MAX_ATTEMPTS, payload={"strategy_id": str(sched.strategy_id), "scan_run_id": str(uuid4()),
                                                                                  "trigger": "schedule", "candle_close": close.isoformat()})
        sched.last_enqueued_at, sched.last_job_id, sched.last_skip_reason = now, job.id, None if created else "already_enqueued_for_this_candle"
        stats["enqueued"] += int(created)
    db.commit()
    return stats


def request_estimate(db: Session, user_id: UUID) -> dict[str, object]:
    """Expected provider request volume of scheduled scans. A scan reads STORED 1-minute bars and stored option quotes only, so it issues no
    provider request; freshness depends on the separate ingestion loops (which carry their own, rate-limited volume)."""
    rows = []
    for sched in db.scalars(select(ScannerSchedule).where(ScannerSchedule.user_id == user_id, ScannerSchedule.enabled.is_(True))):
        rows.append({"strategy_id": str(sched.strategy_id), "cadence_minutes": sched.cadence_minutes, "scans_per_session": EQUITY_IN.session_minutes // sched.cadence_minutes})
    return {"provider_requests_per_scan_cycle": 0, "enabled_schedules": rows,
            "basis": "Scans read stored bars and stored option quotes from the database; the scan path contains no provider HTTP call.",
            "dhan_documented_limits_for_reference": "Data APIs 5 requests/second; option chain 1 request per 3 seconds (documented by Dhan; not re-verified here)."}

