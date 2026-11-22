"""Dated stock verdicts (Phase 7), one row per owner, stock, day and horizon, written nightly in shadow mode whatever the flag says. Additive (migration 0069)."""
from datetime import date, datetime
from uuid import UUID, uuid4

from sqlalchemy import Date, DateTime, Float, ForeignKey, Index, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models import JSON_DOCUMENT


class StockVerdict(Base):
    __tablename__ = "stock_verdicts"
    __table_args__ = (UniqueConstraint("instrument_id", "user_id", "as_of", "horizon", name="uq_stock_verdict_day"), Index("ix_stock_verdicts_user_day", "user_id", "as_of"))

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    instrument_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("instruments.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    as_of: Mapped[date] = mapped_column(Date, nullable=False)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    horizon: Mapped[str] = mapped_column(String(12), nullable=False)
    verdict: Mapped[str] = mapped_column(String(16), nullable=False)
    evidence_score: Mapped[float | None] = mapped_column(Float)
    calibrated_confidence: Mapped[float | None] = mapped_column(Float)
    lens: Mapped[dict] = mapped_column(JSON_DOCUMENT, nullable=False)           # the seven lenses as seen for this horizon (scores, directions, facts)
    reasons: Mapped[dict] = mapped_column(JSON_DOCUMENT, nullable=False)        # top reasons, veto, size, levels, evidence parts, risks and events
    model_version: Mapped[str] = mapped_column(String(48), nullable=False)
