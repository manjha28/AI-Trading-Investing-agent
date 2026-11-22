"""Append-only log of Yahoo statement-fetch attempts, so a rate-limited fetch ("not fetched, retry queued") is never confused with a stock that genuinely has no statements ("no statements exist").
The retry queue is derived from this log plus the stored statements (see services/fundamentals_backfill.py)."""
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

FETCH_OUTCOMES = ("refreshed", "rate_limited", "no_statements", "failed")


class FundamentalsFetchLog(Base):
    __tablename__ = "fundamentals_fetch_log"
    __table_args__ = (CheckConstraint("outcome IN ('refreshed', 'rate_limited', 'no_statements', 'failed')", name="ck_fundamentals_fetch_log_outcome"),
                      Index("ix_fundamentals_fetch_log_instrument_time", "instrument_id", "attempted_at"))

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    instrument_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("instruments.id", ondelete="CASCADE"), nullable=False)
    attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="yfinance")
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    annual_periods: Mapped[int | None] = mapped_column(Integer)
    quarterly_periods: Mapped[int | None] = mapped_column(Integer)
    detail: Mapped[str | None] = mapped_column(String(300))
    origin: Mapped[str] = mapped_column(String(24), nullable=False, default="research_run")      # research_run | backfill
