from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models import JSON_DOCUMENT


class AlertRule(Base):
    __tablename__ = "alert_rules"
    __table_args__ = (
        CheckConstraint(
            "scope IN ('instrument', 'position', 'portfolio')",
            name="ck_alert_rules_scope",
        ),
        CheckConstraint(
            "severity IN ('info', 'attention', 'urgent')",
            name="ck_alert_rules_severity",
        ),
        CheckConstraint("cooldown_seconds >= 0", name="ck_alert_rules_cooldown"),
        CheckConstraint(
            "(scope = 'instrument' AND instrument_id IS NOT NULL) OR "
            "(scope = 'position' AND portfolio_id IS NOT NULL AND instrument_id IS NOT NULL) OR "
            "(scope = 'portfolio' AND portfolio_id IS NOT NULL)",
            name="ck_alert_rules_scope_target",
        ),
        Index("ix_alert_rules_user_enabled", "user_id", "enabled"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    scope: Mapped[str] = mapped_column(String(12), nullable=False)
    instrument_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("instruments.id", ondelete="CASCADE")
    )
    portfolio_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("portfolios.id", ondelete="CASCADE")
    )
    rule_type: Mapped[str] = mapped_column(String(40), nullable=False)
    params: Mapped[dict] = mapped_column(JSON_DOCUMENT, nullable=False)
    severity: Mapped[str] = mapped_column(String(12), nullable=False)
    cooldown_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=3600)
    enabled: Mapped[bool] = mapped_column(nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AlertEvent(Base):
    __tablename__ = "alert_events"
    __table_args__ = (
        CheckConstraint(
            "status IN ('new', 'read', 'dismissed', 'actioned')",
            name="ck_alert_events_status",
        ),
        UniqueConstraint("rule_id", "dedupe_key", name="uq_alert_events_rule_dedupe"),
        Index("ix_alert_events_user_fired", "user_id", "fired_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    rule_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("alert_rules.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    instrument_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("instruments.id", ondelete="SET NULL")
    )
    fired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(160), nullable=False)
    evidence: Mapped[dict] = mapped_column(JSON_DOCUMENT, nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="new")
