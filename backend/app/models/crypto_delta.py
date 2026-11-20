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
