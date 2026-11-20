from datetime import date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models import JSON_DOCUMENT


class ForwardTest(Base):
    """A strategy registered to run on LIVE market data from ``start_date``. By default it is a paper test: hypothetical trades at a
    fixed quantity, replayed causally on completed candles, never mixed with real P&L. ``execute_live`` additionally lets the Dhan
    execution layer place real orders (only when every independent live safeguard allows it)."""

    __tablename__ = "forward_tests"
    __table_args__ = (Index("ix_forward_tests_user_created", "user_id", "created_at"),)

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    strategy_key: Mapped[str] = mapped_column(String(64), nullable=False)
    definition: Mapped[dict] = mapped_column(JSON_DOCUMENT, nullable=False)      # frozen at creation: the test is reproducible
    definition_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    instrument_ids: Mapped[list] = mapped_column(JSON_DOCUMENT, nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="active")   # active | paused
    execute_live: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    slippage_bps: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False, default=Decimal("2"))
    cost_profile: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    snapshot: Mapped[dict | None] = mapped_column(JSON_DOCUMENT)


class DhanLiveState(Base):
    """Per-user live-execution arming and limits. Absent or ``armed = false`` means no order can ever be sent for this user."""

    __tablename__ = "dhan_live_state"

    user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    armed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    armed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    disarmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    max_daily_loss_inr: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, default=Decimal("500"))
    max_trades_per_day: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    max_open_positions: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    max_notional_inr: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, default=Decimal("50000"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DhanLiveOrder(Base):
    """Every live order this app tried to place, with the broker's actual reply. Owner-scoped; never mixed with paper/backtest rows."""

    __tablename__ = "dhan_live_orders"
    __table_args__ = (
        UniqueConstraint("client_order_id", name="uq_dhan_live_orders_client_id"),
        Index("ix_dhan_live_orders_user_created", "user_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    forward_test_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("forward_tests.id", ondelete="SET NULL"))
    strategy_key: Mapped[str] = mapped_column(String(64), nullable=False)
    instrument_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("instruments.id", ondelete="CASCADE"), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    security_id: Mapped[str] = mapped_column(String(32), nullable=False)
    side: Mapped[str] = mapped_column(String(4), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    signal_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reference_price: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    stop_price: Mapped[Decimal | None] = mapped_column(Numeric(24, 8))
    target_price: Mapped[Decimal | None] = mapped_column(Numeric(24, 8))
    client_order_id: Mapped[str] = mapped_column(String(30), nullable=False)
    dhan_order_id: Mapped[str | None] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(24), nullable=False)   # blocked | placed | rejected | filled | closed | cancelled | error
    block_reason: Mapped[str | None] = mapped_column(String(200))
    entry_price: Mapped[Decimal | None] = mapped_column(Numeric(24, 8))
    exit_price: Mapped[Decimal | None] = mapped_column(Numeric(24, 8))
    exit_reason: Mapped[str | None] = mapped_column(String(40))
    realized_pnl: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    request: Mapped[dict | None] = mapped_column(JSON_DOCUMENT)       # what was sent (no credentials)
    response: Mapped[dict | None] = mapped_column(JSON_DOCUMENT)      # what Dhan answered, verbatim
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
