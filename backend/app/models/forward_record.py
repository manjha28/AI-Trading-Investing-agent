"""Weekly forward-record reports (Phase 7b): what the nightly shadow verdicts and multibagger classes actually did afterwards, from the point-in-time shadow tables only. One stored report per owner and week. Additive (migration 0071)."""
from datetime import date, datetime
from uuid import UUID, uuid4

from sqlalchemy import Date, DateTime, ForeignKey, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models import JSON_DOCUMENT


class ForwardRecordReport(Base):
    __tablename__ = "forward_record_reports"
    __table_args__ = (UniqueConstraint("user_id", "as_of", name="uq_forward_record_day"),)

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    as_of: Mapped[date] = mapped_column(Date, nullable=False)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON_DOCUMENT, nullable=False)
