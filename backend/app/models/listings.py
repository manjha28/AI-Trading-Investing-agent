"""Issue price and key dates of recent listings (Phase 6 Stage 7c). Manual entry is fine for now (the NSE IPO adapter can fill it later); every field is nullable: unknown is never zero."""
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import Date, DateTime, ForeignKey, Numeric, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class InstrumentListing(Base):
    __tablename__ = "instrument_listings"
    __table_args__ = (UniqueConstraint("instrument_id", name="uq_instrument_listings_instrument"),)

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    instrument_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("instruments.id", ondelete="CASCADE"), nullable=False)
    issue_price: Mapped[Decimal | None] = mapped_column(Numeric(24, 8))
    allotment_date: Mapped[date | None] = mapped_column(Date)
    listing_date: Mapped[date | None] = mapped_column(Date)
    pre_ipo_lockin_end: Mapped[date | None] = mapped_column(Date)                 # main pre-IPO lock-in expiry, when known (otherwise estimated from the allotment date and labelled approximate)
    source: Mapped[str] = mapped_column(String(64), nullable=False, default="manual")
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
