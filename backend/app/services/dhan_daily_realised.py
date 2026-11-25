"""Capture Dhan's realised profit for the current IST day from a successful positions fetch (see app.models.dhan_daily_realised)."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.dhan_daily_realised import DhanDailyRealised

IST = ZoneInfo("Asia/Kolkata")


def record(db: Session, user_id: UUID, positions: list, synced_at: datetime) -> DhanDailyRealised:
    """`positions` are the normalised BrokerMirror position rows of ONE successful fetch (empty = Dhan listed no position today, so realised is 0)."""
    day: date = synced_at.astimezone(IST).date()
    known = [p.realized_profit for p in positions if p.realized_profit is not None]
    realised = sum(known, Decimal(0)) if len(known) == len(positions) else None      # a position without Dhan's figure makes the day's total unknown, never a guess
    by_contract = [{"symbol": p.symbol, "security_id": p.security_id, "realised": None if p.realized_profit is None else format(p.realized_profit, "f")} for p in positions]
    row = db.scalar(select(DhanDailyRealised).where(DhanDailyRealised.user_id == user_id, DhanDailyRealised.day == day))
    if row is None:
        row = DhanDailyRealised(user_id=user_id, day=day, realised=realised, positions_count=len(positions), by_contract=by_contract, synced_at=synced_at)
        db.add(row)
    else:
        row.realised, row.positions_count, row.by_contract, row.synced_at = realised, len(positions), by_contract, synced_at
    return row


def for_day(db: Session, user_id: UUID, day: date) -> DhanDailyRealised | None:
    return db.scalar(select(DhanDailyRealised).where(DhanDailyRealised.user_id == user_id, DhanDailyRealised.day == day))
