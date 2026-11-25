"""Dhan's own realised profit for one trading day (IST), captured from the positions endpoint while Dhan still lists that day.

Dhan's positions endpoint only returns the CURRENT day, and the live mirror (broker_mirror) drops a day's rows at the next sync after it ends,
so without this record the reconcile page could never compare a past day against Dhan's number. Gross: before brokerage, STT, exchange charges,
GST and stamp duty (Dhan's basis). One row per user and day; each sync that day overwrites it with Dhan's latest figure."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import Date, DateTime, ForeignKey, Integer, Numeric, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.models import JSON_DOCUMENT, Base


class DhanDailyRealised(Base):
    __tablename__ = "dhan_daily_realised"
    __table_args__ = (UniqueConstraint("user_id", "day", name="uq_dhan_daily_realised_user_day"),)

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    day: Mapped[date] = mapped_column(Date, nullable=False)                       # IST trading day the positions belong to
    realised: Mapped[Decimal | None] = mapped_column(Numeric(24, 8))             # sum of Dhan realizedProfit; None if any position lacked it
    positions_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    by_contract: Mapped[list] = mapped_column(JSON_DOCUMENT, nullable=False)     # [{symbol, security_id, realised}] as Dhan reported them (strings)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
