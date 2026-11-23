from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, Numeric, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models import JSON_DOCUMENT


class CryptoCandle(Base):
    """A COMPLETED public candle from Delta's history endpoint. Separate from Indian ``market_bars``."""
    __tablename__ = "crypto_candles"
    __table_args__ = (
        UniqueConstraint("symbol", "resolution", "open_time", name="uq_crypto_candles_key"),
        Index("ix_crypto_candles_lookup", "symbol", "resolution", "open_time"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    resolution: Mapped[str] = mapped_column(String(4), nullable=False)
    open_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    open: Mapped[Decimal] = mapped_column(Numeric(28, 10), nullable=False)
    high: Mapped[Decimal] = mapped_column(Numeric(28, 10), nullable=False)
    low: Mapped[Decimal] = mapped_column(Numeric(28, 10), nullable=False)
    close: Mapped[Decimal] = mapped_column(Numeric(28, 10), nullable=False)
    volume: Mapped[Decimal] = mapped_column(Numeric(28, 10), nullable=False)
    source: Mapped[str] = mapped_column(String(40), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CryptoBacktestRun(Base):
    """A stored, owner-scoped backtest. Always hypothetical; never mixed with real portfolio P&L."""
    __tablename__ = "crypto_backtest_runs"
    __table_args__ = (Index("ix_crypto_backtest_user_created", "user_id", "created_at"),)

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    strategy_id: Mapped[str] = mapped_column(String(32), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    resolution: Mapped[str] = mapped_column(String(4), nullable=False)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    bars_used: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # completed | no_data
    hypothetical: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    params: Mapped[dict] = mapped_column(JSON_DOCUMENT, nullable=False)
    assumptions: Mapped[dict] = mapped_column(JSON_DOCUMENT, nullable=False)
    data_quality: Mapped[dict] = mapped_column(JSON_DOCUMENT, nullable=False)
    metrics: Mapped[dict] = mapped_column(JSON_DOCUMENT, nullable=False)
    trades: Mapped[list] = mapped_column(JSON_DOCUMENT, nullable=False)
    equity: Mapped[list] = mapped_column(JSON_DOCUMENT, nullable=False)


class CryptoForwardTest(Base):
    """A paper forward test of the 5 EMA alert-candle strategy on a Delta perpetual. Hypothetical only; no order path to Delta exists."""
    __tablename__ = "crypto_forward_tests"
    __table_args__ = (Index("ix_crypto_forward_user_created", "user_id", "created_at"),)

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    resolution: Mapped[str] = mapped_column(String(4), nullable=False)
    params: Mapped[dict] = mapped_column(JSON_DOCUMENT, nullable=False)
    contracts: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    slippage_bps: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False, default=Decimal("5"))
    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="active")   # active | paused
    execute_live: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")   # layer 3 of 3 for Delta live orders
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(400))
    snapshot: Mapped[dict | None] = mapped_column(JSON_DOCUMENT)


class DeltaLiveState(Base):
    """Per-user Delta live-execution arming and limits. Absent or ``armed = false`` means no order can ever be sent for this user."""
    __tablename__ = "delta_live_state"

    user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    armed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    armed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    disarmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    max_daily_loss_usd: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, default=Decimal("25"))
    max_trades_per_day: Mapped[int] = mapped_column(Integer, nullable=False, default=6)
    max_open_positions: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    max_notional_usd: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, default=Decimal("2000"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DeltaLiveOrder(Base):
    """Every live Delta order this app tried to place, with the broker's actual reply. Owner-scoped; never mixed with paper rows."""
    __tablename__ = "delta_live_orders"
    __table_args__ = (UniqueConstraint("client_order_id", name="uq_delta_live_orders_client_id"), Index("ix_delta_live_orders_user_created", "user_id", "created_at"))

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    forward_test_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("crypto_forward_tests.id", ondelete="SET NULL"))
    strategy_key: Mapped[str] = mapped_column(String(64), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    product_id: Mapped[int] = mapped_column(Integer, nullable=False)
    side: Mapped[str] = mapped_column(String(4), nullable=False)
    contracts: Mapped[int] = mapped_column(Integer, nullable=False)
    leverage: Mapped[int] = mapped_column(Integer, nullable=False)
    signal_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reference_price: Mapped[Decimal] = mapped_column(Numeric(28, 10), nullable=False)
    stop_price: Mapped[Decimal | None] = mapped_column(Numeric(28, 10))
    target_price: Mapped[Decimal | None] = mapped_column(Numeric(28, 10))
    client_order_id: Mapped[str] = mapped_column(String(30), nullable=False)
    delta_order_id: Mapped[str | None] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(24), nullable=False)   # blocked | placed | rejected | filled | closed | cancelled | error
    block_reason: Mapped[str | None] = mapped_column(String(300))
    entry_price: Mapped[Decimal | None] = mapped_column(Numeric(28, 10))
    request: Mapped[dict | None] = mapped_column(JSON_DOCUMENT)       # what was sent (no credentials)
    response: Mapped[dict | None] = mapped_column(JSON_DOCUMENT)      # what Delta answered, verbatim
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
