"""The daily research report, built once after each research run and stored (one row per owner), so the endpoint returns a stored copy instead of rebuilding every card on each request. Additive (migration 0068)."""
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, Float, ForeignKey, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models import JSON_DOCUMENT


class DailyReportSnapshot(Base):
    __tablename__ = "daily_report_snapshots"
    __table_args__ = (UniqueConstraint("user_id", name="uq_daily_report_snapshots_user"),)

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    built_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    build_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    etag: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON_DOCUMENT, nullable=False)
