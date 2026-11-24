"""Refresh the Dhan holdings/positions snapshot in the BACKGROUND, a few minutes apart, while the market is open.

Opening a page or switching views used to fire POST /brokers/dhan/sync?snapshot_only=true every time. The snapshot is now kept current here (at most every
``dhan_snapshot_refresh_seconds``, only in market hours and shortly after), and the Sync button remains the explicit way to ask for more. An expired Dhan session is
skipped, never retried in a loop.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from types import SimpleNamespace

from fastapi import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import engine
from app.models import BrokerCredential

logger = logging.getLogger("dhan_snapshot_refresh")


def run_pass(now: datetime | None = None) -> dict[str, int]:
    from app.api.brokers import sync_broker
    from app.services import forward_test as fts

    now = now or datetime.now(timezone.utc)
    local = now.astimezone(fts.IST)
    window = local.weekday() < 5 and (fts.session_open(now) or (local.hour, local.minute) < (16, 0) and (local.hour, local.minute) >= (9, 0))
    if not window:
        return {"users": 0, "refreshed": 0, "skipped_expired": 0, "errors": 0}
    refreshed = expired = errors = users = 0
    with Session(engine) as db:
        for credential in list(db.scalars(select(BrokerCredential).where(BrokerCredential.broker == "dhan"))):
            expiry = credential.expires_at if credential.expires_at.tzinfo else credential.expires_at.replace(tzinfo=timezone.utc)
            if expiry <= now:
                expired += 1
                continue
            users += 1
            try:
                sync_broker(slug="dhan", response=Response(), from_date=None, to_date=None, snapshot_only=True, active_session=SimpleNamespace(user_id=credential.user_id), _=None, db=db)
                refreshed += 1
            except Exception:  # noqa: BLE001 - recorded; the next pass tries again
                db.rollback()
                errors += 1
    return {"users": users, "refreshed": refreshed, "skipped_expired": expired, "errors": errors}


async def dhan_snapshot_refresh_loop() -> None:
    await asyncio.sleep(60)                                    # let the app finish starting
    while True:
        try:
            if settings.dhan_snapshot_refresh_enabled:
                await asyncio.to_thread(run_pass)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("dhan_snapshot_refresh_failed")
        await asyncio.sleep(settings.dhan_snapshot_refresh_seconds)
