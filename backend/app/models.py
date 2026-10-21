from datetime import date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, Date, DateTime, ForeignKey, Index, JSON, Numeric, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class User(Base):
    __tablename__ = "users"
    __table_args__ = (Index("ix_users_email", "email", unique=True),)

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    email_verified: Mapped[bool] = mapped_column(default=False, nullable=False)
    two_factor_secret_encrypted: Mapped[str | None] = mapped_column(Text)
    two_factor_enabled: Mapped[bool] = mapped_column(default=False, nullable=False)
    two_factor_last_counter: Mapped[int | None] = mapped_column()
    two_factor_failed_attempts: Mapped[int] = mapped_column(default=0, nullable=False)
    two_factor_locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sessions: Mapped[list["UserSession"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class UserSession(Base):
    __tablename__ = "user_sessions"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    csrf_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    two_factor_verified: Mapped[bool] = mapped_column(default=False, nullable=False)
    user: Mapped[User] = relationship(back_populates="sessions")


class EmailVerificationToken(Base):
    __tablename__ = "email_verification_tokens"
    __table_args__ = (
        Index("ix_email_verification_tokens_user_created", "user_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Portfolio(Base):
    __tablename__ = "portfolios"
    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_portfolios_user_name"),
        Index("ix_portfolios_user_id", "user_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    base_currency: Mapped[str] = mapped_column(String(3), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PortfolioTransaction(Base):
    __tablename__ = "portfolio_transactions"
    __table_args__ = (
        Index("ix_portfolio_transactions_portfolio_time", "portfolio_id", "occurred_at"),
        CheckConstraint(
            "transaction_type IN ('deposit', 'withdrawal', 'buy', 'sell', 'dividend')",
            name="ck_portfolio_transactions_type",
        ),
        CheckConstraint("amount >= 0 AND fee >= 0", name="ck_portfolio_transactions_amounts"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    portfolio_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("portfolios.id", ondelete="CASCADE"), nullable=False
    )
    instrument_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("instruments.id", ondelete="SET NULL"), index=True
    )
    transaction_type: Mapped[str] = mapped_column(String(16), nullable=False)
    symbol: Mapped[str | None] = mapped_column(String(32))
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(24, 8))
    unit_price: Mapped[Decimal | None] = mapped_column(Numeric(24, 8))
    amount: Mapped[Decimal] = mapped_column(Numeric(24, 2), nullable=False)
    fee: Mapped[Decimal] = mapped_column(Numeric(24, 2), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class JournalTrade(Base):
    __tablename__ = "journal_trades"
    __table_args__ = (
        Index("ix_journal_trades_user_exit_time", "user_id", "exit_time"),
        CheckConstraint("asset_class IN ('equity', 'future', 'option', 'crypto')", name="ck_journal_asset_class"),
        CheckConstraint("side IN ('long', 'short')", name="ck_journal_side"),
        CheckConstraint("currency IN ('USD', 'INR')", name="ck_journal_currency"),
        CheckConstraint("quantity > 0 AND multiplier > 0 AND entry_price > 0", name="ck_journal_trade_positive_values"),
        CheckConstraint("fees >= 0", name="ck_journal_trade_fees"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    asset_class: Mapped[str] = mapped_column(String(12), nullable=False)
    instrument: Mapped[str] = mapped_column(String(64), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    multiplier: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    entry_price: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    exit_price: Mapped[Decimal | None] = mapped_column(Numeric(24, 8))
    entry_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    exit_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fees: Mapped[Decimal] = mapped_column(Numeric(24, 2), nullable=False)
    planned_risk: Mapped[Decimal | None] = mapped_column(Numeric(24, 2))
    strategy: Mapped[str | None] = mapped_column(String(100))
    setup: Mapped[str | None] = mapped_column(String(100))
    notes: Mapped[str | None] = mapped_column(String(4000))
    tags: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    option_type: Mapped[str | None] = mapped_column(String(4))
    strike: Mapped[Decimal | None] = mapped_column(Numeric(24, 8))
    expiry: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Instrument(Base):
    __tablename__ = "instruments"
    __table_args__ = (
        CheckConstraint(
            "asset_class IN ('equity', 'etf', 'index', 'future', 'option', 'crypto')",
            name="ck_instruments_asset_class",
        ),
        CheckConstraint("length(currency) IN (3, 4)", name="ck_instruments_currency_length"),
        CheckConstraint(
            "(asset_class = 'option' AND expiry IS NOT NULL AND strike IS NOT NULL "
            "AND option_type IN ('call', 'put')) OR "
            "(asset_class != 'option' AND strike IS NULL AND option_type IS NULL)",
            name="ck_instruments_option_metadata",
        ),
        CheckConstraint(
            "(asset_class IN ('future', 'option') AND multiplier IS NOT NULL AND multiplier > 0) "
            "OR (asset_class NOT IN ('future', 'option') AND multiplier IS NULL)",
            name="ck_instruments_derivative_multiplier",
        ),
        CheckConstraint(
            "asset_class != 'future' OR expiry IS NOT NULL",
            name="ck_instruments_future_expiry",
        ),
        Index("ix_instruments_symbol_venue", "symbol", "venue"),
        Index("ix_instruments_name", "display_name"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    display_name: Mapped[str] = mapped_column(String(160), nullable=False)
    asset_class: Mapped[str] = mapped_column(String(12), nullable=False)
    venue: Mapped[str] = mapped_column(String(32), nullable=False)
    currency: Mapped[str] = mapped_column(String(4), nullable=False)
    country: Mapped[str] = mapped_column(String(2), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    multiplier: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    expiry: Mapped[date | None] = mapped_column(Date)
    strike: Mapped[Decimal | None] = mapped_column(Numeric(24, 8))
    option_type: Mapped[str | None] = mapped_column(String(4))
    is_synthetic: Mapped[bool] = mapped_column(default=False, nullable=False)
    relink_required: Mapped[bool] = mapped_column(default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    provider_identifiers: Mapped[list["InstrumentProviderIdentifier"]] = relationship(
        back_populates="instrument", cascade="all, delete-orphan"
    )
    watchlist_entries: Mapped[list["WatchlistInstrument"]] = relationship(
        back_populates="instrument", cascade="all, delete-orphan"
    )


class WatchlistInstrument(Base):
    __tablename__ = "watchlist_instruments"
    __table_args__ = (
        UniqueConstraint("user_id", "instrument_id", name="uq_watchlist_user_instrument"),
        Index("ix_watchlist_instruments_instrument", "instrument_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    instrument_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("instruments.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    instrument: Mapped[Instrument] = relationship(back_populates="watchlist_entries")


class InstrumentProviderIdentifier(Base):
    __tablename__ = "instrument_provider_identifiers"
    __table_args__ = (
        UniqueConstraint(
            "provider", "provider_instrument_id", name="uq_provider_instrument_identifier"
        ),
        Index("ix_instrument_provider_identifiers_instrument", "instrument_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    instrument_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("instruments.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_instrument_id: Mapped[str] = mapped_column(String(128), nullable=False)
    provider_symbol: Mapped[str] = mapped_column(String(96), nullable=False)
    is_verified: Mapped[bool] = mapped_column(default=False, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    instrument: Mapped[Instrument] = relationship(back_populates="provider_identifiers")


class MarketBar(Base):
    __tablename__ = "market_bars"
    __table_args__ = (
        UniqueConstraint(
            "instrument_id",
            "interval",
            "timestamp",
            "source",
            name="uq_market_bars_instrument_interval_time_source",
        ),
        CheckConstraint("interval IN ('1d', '1h', '15m', '1m')", name="ck_market_bars_interval"),
        CheckConstraint(
            "low <= high AND low <= open AND low <= close "
            "AND high >= open AND high >= close",
            name="ck_market_bars_ohlc_relationship",
        ),
        CheckConstraint(
            "open > 0 AND high > 0 AND low > 0 AND close > 0",
            name="ck_market_bars_positive_prices",
        ),
        CheckConstraint("volume >= 0", name="ck_market_bars_nonnegative_volume"),
        Index("ix_market_bars_instrument_interval_time", "instrument_id", "interval", "timestamp"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    instrument_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("instruments.id", ondelete="CASCADE"), nullable=False
    )
    interval: Mapped[str] = mapped_column(String(8), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    open: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    high: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    low: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    close: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    volume: Mapped[Decimal] = mapped_column(Numeric(30, 8), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    is_synthetic: Mapped[bool] = mapped_column(default=False, nullable=False)


class MarketQuote(Base):
    __tablename__ = "market_quotes"
    __table_args__ = (
        UniqueConstraint(
            "instrument_id",
            "source",
            name="uq_market_quotes_instrument_source",
        ),
        CheckConstraint(
            "low <= high AND low <= open AND low <= close "
            "AND high >= open AND high >= close",
            name="ck_market_quotes_ohlc_relationship",
        ),
        CheckConstraint(
            "open > 0 AND high > 0 AND low > 0 AND close > 0",
            name="ck_market_quotes_positive_prices",
        ),
        CheckConstraint("volume >= 0", name="ck_market_quotes_nonnegative_volume"),
        Index("ix_market_quotes_instrument_retrieved", "instrument_id", "retrieved_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    instrument_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("instruments.id", ondelete="CASCADE"), nullable=False
    )
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    open: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    high: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    low: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    close: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    volume: Mapped[Decimal] = mapped_column(Numeric(30, 8), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    is_synthetic: Mapped[bool] = mapped_column(default=False, nullable=False)


class MarketDataSync(Base):
    __tablename__ = "market_data_syncs"
    __table_args__ = (
        UniqueConstraint("instrument_id", "interval", "source", name="uq_market_sync_instrument"),
        CheckConstraint("interval IN ('1d', '1h', '15m', '1m')", name="ck_market_sync_interval"),
        CheckConstraint("status IN ('success', 'unavailable')", name="ck_market_sync_status"),
        Index("ix_market_sync_instrument_interval", "instrument_id", "interval"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    instrument_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("instruments.id", ondelete="CASCADE"), nullable=False
    )
    interval: Mapped[str] = mapped_column(String(8), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    succeeded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    rows_received: Mapped[int] = mapped_column(default=0, nullable=False)
    safe_error_code: Mapped[str | None] = mapped_column(String(48))


class BrokerCredential(Base):
    __tablename__ = "broker_credentials"
    __table_args__ = (
        UniqueConstraint("user_id", "broker", name="uq_broker_credentials_user_broker"),
        Index("ix_broker_credentials_user_id", "user_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    broker: Mapped[str] = mapped_column(String(32), nullable=False)
    encrypted_tokens: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    connected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BrokerOAuthState(Base):
    __tablename__ = "broker_oauth_states"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    broker: Mapped[str] = mapped_column(String(32), nullable=False)
    state_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    metadata_json: Mapped[dict[str, str] | None] = mapped_column(JSON)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)


class BrokerMirror(Base):
    __tablename__ = "broker_mirror"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "broker",
            "kind",
            "symbol",
            "exchange",
            "product",
            name="uq_broker_mirror_position",
        ),
        CheckConstraint("kind IN ('holding', 'position')", name="ck_broker_mirror_kind"),
        Index("ix_broker_mirror_user_broker", "user_id", "broker"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    broker: Mapped[str] = mapped_column(String(32), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    exchange: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    product: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    quantity: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    average_price: Mapped[Decimal | None] = mapped_column(Numeric(24, 8))
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)