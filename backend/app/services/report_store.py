"""Stored daily report: build once after each research run (and in the background when the stored copy is stale), serve the stored copy with an ETag. The endpoint never rebuilds the report inside a
request except on the very first use (nothing stored yet)."""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)
MAX_AGE = timedelta(minutes=10)           # a stored report older than this is rebuilt in the background (holdings and prices move between research runs)
_building: set[UUID] = set()
_lock = threading.Lock()


def _jsonable(obj: Any) -> Any:
    return json.loads(json.dumps(obj, default=str))


def build_and_store(db: Session, user_id: UUID, now: datetime | None = None) -> dict[str, Any]:
    from app.models.report_snapshot import DailyReportSnapshot
    from app.services import research_report
    now = now or datetime.now(timezone.utc)
    t0 = time.perf_counter()
    report = _jsonable(research_report.build_report(db, user_id, now))
    seconds = time.perf_counter() - t0
    etag = hashlib.sha256(json.dumps({k: v for k, v in report.items() if k != "as_of"}, sort_keys=True).encode()).hexdigest()[:32]
    row = db.scalar(select(DailyReportSnapshot).where(DailyReportSnapshot.user_id == user_id))
    if row is None:
        db.add(DailyReportSnapshot(user_id=user_id, built_at=now, build_seconds=seconds, etag=etag, payload=report))
    else:
        row.built_at, row.build_seconds, row.etag, row.payload = now, seconds, etag, report
    db.commit()
    return {"payload": report, "etag": etag, "built_at": now, "build_seconds": seconds}


def refresh_in_background(user_id: UUID) -> bool:
    """Start one background rebuild for this owner unless one is running. Returns True if it started."""
    with _lock:
        if user_id in _building:
            return False
        _building.add(user_id)

    def run() -> None:
        try:
            from app.db import engine
            with Session(engine) as db:
                build_and_store(db, user_id)
        except Exception as exc:  # noqa: BLE001 - the stored copy keeps serving
            logger.warning("daily_report_rebuild_failed error_class=%s", type(exc).__name__)
        finally:
            with _lock:
                _building.discard(user_id)
    threading.Thread(target=run, name="daily-report-build", daemon=True).start()
    return True


def serve(db: Session, user_id: UUID, now: datetime | None = None) -> dict[str, Any]:
    """-> {payload, etag, built_at, rebuilding}. Builds synchronously only when nothing is stored yet."""
    from app.models import InstrumentResearchRun
    from app.models.report_snapshot import DailyReportSnapshot
    now = now or datetime.now(timezone.utc)
    row = db.scalar(select(DailyReportSnapshot).where(DailyReportSnapshot.user_id == user_id))
    if row is None:
        built = build_and_store(db, user_id, now)
        return {**built, "rebuilding": False}
    built_at = row.built_at if row.built_at.tzinfo else row.built_at.replace(tzinfo=timezone.utc)
    newest_run = db.scalar(select(func.max(InstrumentResearchRun.finished_at)).where(InstrumentResearchRun.user_id == user_id))
    newest_run = newest_run if newest_run is None or newest_run.tzinfo else newest_run.replace(tzinfo=timezone.utc)
    stale = now - built_at > MAX_AGE or (newest_run is not None and newest_run > built_at)
    rebuilding = refresh_in_background(user_id) if stale else (user_id in _building)
    return {"payload": row.payload, "etag": row.etag, "built_at": built_at, "build_seconds": row.build_seconds, "rebuilding": rebuilding or (stale and user_id in _building)}
