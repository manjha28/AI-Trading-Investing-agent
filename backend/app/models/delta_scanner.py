"""Delta multi-perpetual paper scanner. Structurally paper only: there is NO execute_live column."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, Numeric, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.models import JSON_DOCUMENT, Base


class DeltaScanner(Base):
    __tablename__ = "delta_scanners"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    strategy_key: Mapped[str] = mapped_column(String(32), nullable=False)            # five_ema_alert
    resolution: Mapped[str] = mapped_column(String(4), nullable=False)
    symbols: Mapped[list] = mapped_column(JSON_DOCUMENT, nullable=False)
    params: Mapped[dict] = mapped_column(JSON_DOCUMENT, nullable=False)
    wallet_usd: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    risk_pct: Mapped[Decimal] = mapped_column(Numeric(8, 5), nullable=False)
    total_risk_cap_pct: Mapped[Decimal] = mapped_column(Numeric(8, 5), nullable=False)
    leverage_cap: Mapped[Decimal] = mapped_column(Numeric(8, 2), nullable=False)
    label: Mapped[str] = mapped_column(String(40), nullable=False, default="paper")  # paper | unproven (Part D candidates)
    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(400))
    snapshot: Mapped[dict | None] = mapped_column(JSON_DOCUMENT)
