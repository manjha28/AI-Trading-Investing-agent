"""Scheduled derivatives ingestion (off by default; enable with DERIVATIVES_SCHEDULER_ENABLED / DERIVATIVES_CHAIN_POLL_ENABLED).

- master_refresh_loop: once a day at the configured IST hour, refresh the Dhan instrument master (new/changed/expired/removed contracts are detected, old specs preserved).
- chain_polling_loop: during market hours, snapshot option chains in PRIORITY order (active strategies' underlyings, then the rest), bounded per cycle so the 3-second
  provider limit is respected. It never claims full coverage: it only polls what the cycle budget allows.
Both reuse the shared job engine, so every run is visible, resumable and cancellable from the API.
"""
import asyncio
import logging
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import engine
from app.models import BrokerCredential
from app.services import derivatives_ingest as di

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")


def _user_id():
    with Session(engine) as db:
        cred = db.scalar(select(BrokerCredential).where(BrokerCredential.broker == "dhan"))
        return cred.user_id if cred else None


def _run(kind: str, tasks: list[di.TaskSpec], params: dict, user_id) -> str:
    sf = lambda: Session(engine)  # noqa: E731
    with sf() as db:
        job = di.create_job(db, kind, params, tasks, priority=20, user_id=user_id)
        job_id = job.id
    status = di.run_job(sf, job_id, di.RenewingClient(sf, user_id), sleep=__import__("time").sleep)
    with sf() as db:
        di.refresh_coverage(db)
    return status


def master_refresh_once() -> str | None:
    uid = _user_id()
    if uid is None:
        return None
    return _run("master_refresh", [di.TaskSpec(f"master:{datetime.now(timezone.utc):%Y%m%d%H%M}", "master_refresh", {})], {"scheduled": True}, uid)


def chain_cycle_once() -> str | None:
    """One bounded polling cycle. Returns the job status, or None when Dhan is not connected, the market is closed or nothing is eligible."""
    now = datetime.now(IST)
    if now.weekday() >= 5 or not (time(9, 15) <= now.time() <= time(15, 30)):
        return None
    uid = _user_id()
    if uid is None:
        return None
    with Session(engine) as db:
        tasks = di.plan_chains(db, priority_underlyings=di.strategy_underlyings(db), expiry_count=1, limit=settings.derivatives_chain_poll_max_underlyings)
    if not tasks:
        return None
    return _run("chains_poll", tasks, {"scheduled": True, "max_underlyings": settings.derivatives_chain_poll_max_underlyings}, uid)


async def master_refresh_loop() -> None:
    last_day = None
    while True:
        now = datetime.now(IST)
        if now.hour >= settings.derivatives_master_refresh_hour_ist and last_day != now.date():
            try:
                logger.info("derivatives master refresh: %s", await asyncio.to_thread(master_refresh_once))
                last_day = now.date()
            except Exception:  # noqa: BLE001
                logger.exception("derivatives master refresh failed; will retry next cycle")
        await asyncio.sleep(1800)


async def chain_polling_loop() -> None:
    while True:
        try:
            status = await asyncio.to_thread(chain_cycle_once)
            if status:
                logger.info("derivatives chain polling cycle: %s", status)
        except Exception:  # noqa: BLE001
            logger.exception("derivatives chain polling cycle failed")
        await asyncio.sleep(settings.derivatives_chain_poll_interval_seconds)
