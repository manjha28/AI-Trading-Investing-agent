"""VAR intraday futures research: versioned strategy configs and stored backtest runs (migration 0055).

Backtest runs are HYPOTHETICAL research output, stored apart from real portfolio data and never combined with it."""
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models import JSON_DOCUMENT


class VarConfigVersion(Base):
    __tablename__ = "var_config_versions"
    __table_args__ = (UniqueConstraint("user_id", "version_no", name="uq_var_config_user_version"),)

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    version_no: Mapped[int] = mapped_column(nullable=False)
    config: Mapped[dict] = mapped_column(JSON_DOCUMENT, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class VarBacktestRun(Base):
    __tablename__ = "var_backtest_runs"
    __table_args__ = (
        CheckConstraint("status IN ('queued','running','completed','failed','unavailable','no_usable_data','order_flow_unavailable')", name="ck_var_backtest_status"),
        CheckConstraint("hypothetical = true", name="ck_var_backtest_hypothetical"),
        Index("ix_var_backtest_user_created", "user_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="queued")
    hypothetical: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    config_snapshot: Mapped[dict] = mapped_column(JSON_DOCUMENT, nullable=False)    # exact config used; later edits never rewrite it
    request: Mapped[dict] = mapped_column(JSON_DOCUMENT, nullable=False)             # contracts, capital, window, owner-supplied metadata
    result: Mapped[dict | None] = mapped_column(JSON_DOCUMENT)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
