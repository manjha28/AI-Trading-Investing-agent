"""Nightly multibagger-engine results (Phase 6), one row per owner, stock and day, always written in shadow mode (the screen flag only decides whether they are SHOWN or used). Additive (migration 0066)."""
from datetime import date, datetime
from uuid import UUID, uuid4

from sqlalchemy import Date, DateTime, Float, ForeignKey, Index, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models import JSON_DOCUMENT


class MultibaggerAssessment(Base):
    __tablename__ = "multibagger_assessments"
    __table_args__ = (UniqueConstraint("instrument_id", "user_id", "as_of", name="uq_multibagger_instrument_user_day"), Index("ix_multibagger_user_day", "user_id", "as_of"))

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    instrument_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("instruments.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    as_of: Mapped[date] = mapped_column(Date, nullable=False)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    klass: Mapped[str] = mapped_column("class", String(32), nullable=False)
    q: Mapped[float | None] = mapped_column(Float)
    g: Mapped[float | None] = mapped_column(Float)
    v: Mapped[float | None] = mapped_column(Float)
    t: Mapped[float | None] = mapped_column(Float)
    x: Mapped[float | None] = mapped_column(Float)
    rank: Mapped[float | None] = mapped_column(Float)
    checks: Mapped[dict] = mapped_column(JSON_DOCUMENT, nullable=False)          # the full engine output: checks with reasons, reasons, checklist count, timing tag
    model_version: Mapped[str] = mapped_column(String(48), nullable=False)
