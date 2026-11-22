"""Dated, append-only point-in-time stores (Phase 6 step 1). The live statement and snapshot tables are overwritten on refresh and carry no filing dates, so history built from them would leak the
future; these tables keep what was seen on each date. Rows are never updated or deleted by the app (enforced below); governance_events holds dated events with their source."""
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import Date, DateTime, ForeignKey, Index, Numeric, String, UniqueConstraint, Uuid, event
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models import JSON_DOCUMENT


class FundamentalSnapshot(Base):
    __tablename__ = "fundamental_snapshots"
    __table_args__ = (Index("ix_fundamental_snapshots_instrument_date", "instrument_id", "captured_on"),)

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    instrument_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("instruments.id", ondelete="CASCADE"), nullable=False)
    captured_on: Mapped[date] = mapped_column(Date, nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON_DOCUMENT, nullable=False)         # the statement period rows (with extra_metrics) and provider inputs exactly as seen that day


class ShareholdingSnapshot(Base):
    __tablename__ = "shareholding_snapshots"
    __table_args__ = (UniqueConstraint("instrument_id", "captured_on", "source", name="uq_shareholding_snapshot_day"),)

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    instrument_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("instruments.id", ondelete="CASCADE"), nullable=False)
    captured_on: Mapped[date] = mapped_column(Date, nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    promoter_pct: Mapped[Decimal | None] = mapped_column(Numeric(9, 4))
    fii_pct: Mapped[Decimal | None] = mapped_column(Numeric(9, 4))
    dii_pct: Mapped[Decimal | None] = mapped_column(Numeric(9, 4))
    public_pct: Mapped[Decimal | None] = mapped_column(Numeric(9, 4))
    pledged_pct_of_promoter: Mapped[Decimal | None] = mapped_column(Numeric(9, 4))   # not provided by Dhan: stays NULL (unknown), never 0


class GovernanceEvent(Base):
    __tablename__ = "governance_events"
    __table_args__ = (Index("ix_governance_events_instrument_date", "instrument_id", "event_date"),)

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    instrument_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("instruments.id", ondelete="CASCADE"), nullable=False)
    event_date: Mapped[date] = mapped_column(Date, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)               # auditor_resignation | regulatory_order | pledge_change
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    url: Mapped[str | None] = mapped_column(String(512))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


def _refuse(mapper, connection, target):  # noqa: ANN001
    raise RuntimeError(f"{type(target).__name__} is append-only: rows are never updated or deleted")


for _model in (FundamentalSnapshot, ShareholdingSnapshot):
    event.listen(_model, "before_update", _refuse)
    event.listen(_model, "before_delete", _refuse)
