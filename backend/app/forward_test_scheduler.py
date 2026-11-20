"""Background loop: while the market is open, refresh data and re-run each active forward test; reconcile live orders for users that have any."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import engine
from app.dhan_execution.service import OPEN, reconcile
from app.models.forward_live import DhanLiveOrder, ForwardTest
from app.services import forward_test as ft_service

logger = logging.getLogger("forward_test")


BIG_UNIVERSE = 30


def candle_due(ft: ForwardTest, now: datetime) -> bool:
    """For large lists a pass is expensive, and signals only change when a decision candle closes: run once per candle (15s after the close)."""
    minutes = {"1m": 1, "3m": 3, "5m": 5, "10m": 10, "15m": 15, "30m": 30, "60m": 60}.get((ft.definition or {}).get("decision_timeframe", "5m"), 5)
    local = now.astimezone(ft_service.IST)
    since_open = local.hour * 60 + local.minute - (9 * 60 + 15)
    if since_open < 0:
        return False
    boundary_minutes = (since_open // minutes) * minutes
    boundary = local.replace(hour=9, minute=15, second=0, microsecond=0) + timedelta(minutes=boundary_minutes)
    last = ft_service.aware(ft.last_run_at)
    return now >= boundary.astimezone(timezone.utc) + timedelta(seconds=15) and (last is None or last < boundary.astimezone(timezone.utc) + timedelta(seconds=15))


def should_run(ft: ForwardTest, now: datetime) -> bool:
    if ft.status != "active":
        return False
    if ft.last_run_at is None:
        return True                                           # first pass after creation: warm-up data and a first snapshot
    if ft_service.session_open(now):
        big = ft_service.uses_master(ft) or len(ft.instrument_ids or []) > BIG_UNIVERSE
        return candle_due(ft, now) if big else True
    local = now.astimezone(ft_service.IST)
    after_close = local.weekday() < 5 and local.time() >= time(15, 31)
    last = ft_service.aware(ft.last_run_at).astimezone(ft_service.IST)
    return after_close and last.time() < time(15, 31) and last.date() == local.date()


def run_pass(now: datetime | None = None) -> dict[str, int]:
    now = now or datetime.now(timezone.utc)
    done = failed = 0
    with Session(engine) as db:
        for ft in list(db.scalars(select(ForwardTest).where(ForwardTest.status == "active"))):
            if not should_run(ft, now):
                continue
            result = ft_service.run_once(db, ft, now, client_factory=(lambda uid: ft_service.build_client(db, uid)) if ft.execute_live else None)
            failed += bool(result["error"])
            done += 1
        for user_id in set(db.scalars(select(DhanLiveOrder.user_id).where(DhanLiveOrder.status.in_(OPEN)))):
            try:
                reconcile(db, user_id, lambda uid: ft_service.build_client(db, uid), now=now)
            except Exception:  # noqa: BLE001
                db.rollback()
                logger.exception("dhan_live_reconcile_failed")
    return {"ran": done, "failed": failed}


async def forward_test_loop() -> None:
    while True:
        try:
            if settings.forward_test_enabled:
                await asyncio.to_thread(run_pass)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("forward_test_loop_failed")
        await asyncio.sleep(settings.forward_test_interval_seconds)
