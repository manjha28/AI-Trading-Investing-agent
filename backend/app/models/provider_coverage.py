"""Windows of the trading session that a data provider structurally does not serve for one instrument (for example Dhan serving SBIN
only 09:15-15:14). Kept apart from fetch-failure bookkeeping so a provider limitation is never confused with a missed download."""
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models import JSON_DOCUMENT


class ProviderUnservedWindow(Base):
    __tablename__ = "provider_unserved_windows"
    __table_args__ = (UniqueConstraint("instrument_id", "provider", "interval", "start_minute", "end_minute", name="uq_provider_unserved_window"),
                      Index("ix_provider_unserved_lookup", "instrument_id", "provider", "interval", "status"))

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    instrument_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("instruments.id", ondelete="CASCADE"), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    interval: Mapped[str] = mapped_column(String(8), nullable=False, default="1m")
    start_minute: Mapped[int] = mapped_column(Integer, nullable=False)   # exchange-local minute of day, inclusive (15:15 -> 915)
    end_minute: Mapped[int] = mapped_column(Integer, nullable=False)     # exclusive (15:30 -> 930)
    reason_code: Mapped[str] = mapped_column(String(40), nullable=False, default="provider_tail_not_served")
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="active")   # active | revoked
    sessions_examined: Mapped[int] = mapped_column(Integer, nullable=False)
    sessions_matching: Mapped[int] = mapped_column(Integer, nullable=False)
    evidence: Mapped[dict] = mapped_column(JSON_DOCUMENT, nullable=False, default=dict)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
