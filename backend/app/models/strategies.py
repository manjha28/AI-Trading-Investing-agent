from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models import JSON_DOCUMENT


class Strategy(Base):
    __tablename__ = "strategies"
    __table_args__ = (
        CheckConstraint(
            "status IN ('suspended', 'active', 'retired')",
            name="ck_strategies_status",
        ),
        CheckConstraint("current_version >= 1", name="ck_strategies_current_version"),
        UniqueConstraint("user_id", "name", name="uq_strategies_user_name"),
        Index("ix_strategies_user_status", "user_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    instrument_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("instruments.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(12), nullable=False)
    current_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class StrategyVersion(Base):
    __tablename__ = "strategy_versions"
    __table_args__ = (
        UniqueConstraint("strategy_id", "version", name="uq_strategy_versions_strategy_version"),
        CheckConstraint("version >= 1", name="ck_strategy_versions_version"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    strategy_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("strategies.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    params: Mapped[dict] = mapped_column(JSON_DOCUMENT, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class StrategySignal(Base):
    __tablename__ = "strategy_signals"
    __table_args__ = (
        CheckConstraint("direction IN ('entry', 'exit')", name="ck_strategy_signals_direction"),
        CheckConstraint(
            "status IN ('active', 'rejected', 'expired')",
            name="ck_strategy_signals_status",
        ),
        CheckConstraint(
            "(status = 'rejected' AND rejection_reason IS NOT NULL) OR "
            "(status != 'rejected' AND rejection_reason IS NULL)",
            name="ck_strategy_signals_rejection_reason",
        ),
        CheckConstraint("trigger_price > 0", name="ck_strategy_signals_trigger_price"),
        UniqueConstraint("strategy_id", "dedupe_key", name="uq_strategy_signals_strategy_dedupe"),
        Index("ix_strategy_signals_user_created", "user_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    strategy_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("strategies.id", ondelete="CASCADE"), nullable=False
    )
    strategy_version: Mapped[int] = mapped_column(Integer, nullable=False)
    instrument_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("instruments.id", ondelete="CASCADE"), nullable=False
    )
    direction: Mapped[str] = mapped_column(String(8), nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False)
    rejection_reason: Mapped[str | None] = mapped_column(String(64))
    bar_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    trigger_price: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    rationale: Mapped[str] = mapped_column(String(500), nullable=False)
    evidence: Mapped[dict] = mapped_column(JSON_DOCUMENT, nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(160), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
