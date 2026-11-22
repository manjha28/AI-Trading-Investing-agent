"""Nightly cross-check of stored Dhan daily closes against the NSE bhavcopy close on the same session. A stock whose two closes differ by more than 1% is FLAGGED and excluded from backtests until it
is resolved (the next nightly run finds them within 1% again, or the owner resolves it). Dhan stays the primary price source; the bhavcopy is only the reference. Additive table (migration 0065)."""
from datetime import date, datetime
from uuid import UUID, uuid4

from sqlalchemy import Date, DateTime, Float, ForeignKey, Index, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class PriceCrosscheck(Base):
    __tablename__ = "price_crosscheck"
    __table_args__ = (UniqueConstraint("instrument_id", "session_date", "reference_source", name="uq_price_crosscheck_instrument_session"), Index("ix_price_crosscheck_status", "status"))

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    instrument_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("instruments.id", ondelete="CASCADE"), nullable=False)
    session_date: Mapped[date] = mapped_column(Date, nullable=False)
    dhan_close: Mapped[float] = mapped_column(Float, nullable=False)
    reference_close: Mapped[float] = mapped_column(Float, nullable=False)
    deviation: Mapped[float] = mapped_column(Float, nullable=False)          # dhan / reference - 1
    reference_source: Mapped[str] = mapped_column(String(32), nullable=False, default="nse_bhavcopy")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="flagged")      # flagged | resolved
    first_flagged_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
