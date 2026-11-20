from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Uuid, event
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models import JSON_DOCUMENT


class ExecutionAuditEvent(Base):
    """Append-only record of an order intent, its guard-rail decision, and any (simulated) fill.

    Rows are owner-scoped and written identically for backtest and any future live path. Updates
    and deletes are refused by ORM listeners below; there is no API that rewrites history.
    """
    __tablename__ = "execution_audit_events"
    __table_args__ = (
        Index("ix_execution_audit_user_created", "user_id", "created_at"),
        Index("ix_execution_audit_correlation", "correlation_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    event_type: Mapped[str] = mapped_column(String(16), nullable=False)  # intent | decision | fill
    mode: Mapped[str] = mapped_column(String(12), nullable=False)
    correlation_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    client_order_id: Mapped[str] = mapped_column(String(40), nullable=False)
    venue: Mapped[str] = mapped_column(String(32), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    hypothetical: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    payload: Mapped[dict] = mapped_column(JSON_DOCUMENT, nullable=False)


@event.listens_for(ExecutionAuditEvent, "before_update")
def _refuse_update(*_args) -> None:
    raise RuntimeError("execution_audit_events is append-only")


@event.listens_for(ExecutionAuditEvent, "before_delete")
def _refuse_delete(*_args) -> None:
    raise RuntimeError("execution_audit_events is append-only")
