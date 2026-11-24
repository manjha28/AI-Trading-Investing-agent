"""Last known snapshot per MCX contract, so the commodity watch can show the last session while the market is closed."""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Date, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models import JSON_DOCUMENT, Base


class McxLastSnapshot(Base):
    __tablename__ = "mcx_last_snapshots"

    security_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON_DOCUMENT, nullable=False)   # strings only: ltp, previous_close, open, high, low, volume, oi, tick_time, source
    session_date: Mapped[date | None] = mapped_column(Date)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
