from datetime import date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
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


class InstrumentFundamental(Base):
    __tablename__ = "instrument_fundamentals"
    __table_args__ = (
        UniqueConstraint(
            "instrument_id", "period_type", "period_end",
            name="uq_instrument_fundamentals_period",
        ),
        CheckConstraint("period_type IN ('annual', 'quarterly')", name="ck_instrument_fundamentals_period_type"),
        Index("ix_instrument_fundamentals_instrument_period", "instrument_id", "period_type", "period_end"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    instrument_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("instruments.id", ondelete="CASCADE"), nullable=False
    )
    period_type: Mapped[str] = mapped_column(String(12), nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    currency: Mapped[str] = mapped_column(String(4), nullable=False)
    revenue: Mapped[Decimal | None] = mapped_column(Numeric(24, 8))
    net_income: Mapped[Decimal | None] = mapped_column(Numeric(24, 8))
    eps: Mapped[Decimal | None] = mapped_column(Numeric(24, 8))
    operating_margin: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    total_assets: Mapped[Decimal | None] = mapped_column(Numeric(24, 8))
    total_liabilities: Mapped[Decimal | None] = mapped_column(Numeric(24, 8))
    total_equity: Mapped[Decimal | None] = mapped_column(Numeric(24, 8))
    operating_cash_flow: Mapped[Decimal | None] = mapped_column(Numeric(24, 8))
    free_cash_flow: Mapped[Decimal | None] = mapped_column(Numeric(24, 8))
    total_debt: Mapped[Decimal | None] = mapped_column(Numeric(24, 8))
    shares_outstanding: Mapped[Decimal | None] = mapped_column(Numeric(24, 8))
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class InstrumentSnapshot(Base):
    __tablename__ = "instrument_snapshots"
    __table_args__ = (
        UniqueConstraint("instrument_id", name="uq_instrument_snapshots_instrument"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    instrument_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("instruments.id", ondelete="CASCADE"), nullable=False
    )
    market_cap: Mapped[Decimal | None] = mapped_column(Numeric(28, 8))
    pe: Mapped[Decimal | None] = mapped_column(Numeric(24, 8))
    pb: Mapped[Decimal | None] = mapped_column(Numeric(24, 8))
    dividend_yield: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    sector: Mapped[str | None] = mapped_column(String(200))
    industry: Mapped[str | None] = mapped_column(String(200))
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


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


class CorporateAction(Base):
    __tablename__ = "corporate_actions"
    __table_args__ = (
        CheckConstraint(
            "action_type IN ('split', 'bonus', 'dividend')",
            name="ck_corporate_actions_type",
        ),
        CheckConstraint(
            "(action_type IN ('split', 'bonus') AND ratio IS NOT NULL AND ratio > 0 AND amount IS NULL) "
            "OR (action_type = 'dividend' AND ratio IS NULL AND amount IS NOT NULL AND amount > 0)",
            name="ck_corporate_actions_values",
        ),
        Index("ix_corporate_actions_user_instrument_date", "user_id", "instrument_id", "ex_date"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    instrument_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("instruments.id", ondelete="CASCADE"), nullable=False
    )
    action_type: Mapped[str] = mapped_column(String(12), nullable=False)
    ex_date: Mapped[date] = mapped_column(Date, nullable=False)
    ratio: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    amount: Mapped[Decimal | None] = mapped_column(Numeric(24, 8))
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="manual")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CorporateActionApplication(Base):
    __tablename__ = "corporate_action_applications"
    __table_args__ = (
        Index(
            "uq_corporate_action_applications_active",
            "portfolio_id",
            "corporate_action_id",
            unique=True,
            postgresql_where=text("reversed_at IS NULL"),
            sqlite_where=text("reversed_at IS NULL"),
        ),
        Index(
            "ix_corporate_action_applications_user_portfolio",
            "user_id",
            "portfolio_id",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    portfolio_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("portfolios.id", ondelete="CASCADE"), nullable=False
    )
    corporate_action_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("corporate_actions.id", ondelete="CASCADE"), nullable=False
    )
    applied_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reversed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    affected_quantity: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    quantity_adjustment: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    cash_adjustment: Mapped[Decimal] = mapped_column(Numeric(24, 2), nullable=False)


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


JSON_DOCUMENT = JSON().with_variant(JSONB(), "postgresql")


class ResearchSource(Base):
    __tablename__ = "research_sources"
    __table_args__ = (
        UniqueConstraint("user_id", "content_hash", name="uq_research_sources_user_hash"),
        Index("ix_research_sources_user_instrument", "user_id", "instrument_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    instrument_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("instruments.id", ondelete="SET NULL")
    )
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    publisher: Mapped[str | None] = mapped_column(String(200))
    title: Mapped[str | None] = mapped_column(String(500))
    author: Mapped[str | None] = mapped_column(String(200))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    excerpt: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ResearchNote(Base):
    __tablename__ = "research_notes"
    __table_args__ = (
        CheckConstraint(
            "confidence IN ('low', 'medium', 'high')", name="ck_research_notes_confidence"
        ),
        Index("ix_research_notes_user_instrument", "user_id", "instrument_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    instrument_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("instruments.id", ondelete="CASCADE"), nullable=False
    )
    as_of: Mapped[date] = mapped_column(Date, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    key_facts: Mapped[list] = mapped_column(JSON_DOCUMENT, nullable=False)
    source_ids: Mapped[list] = mapped_column(JSON_DOCUMENT, nullable=False)
    confidence: Mapped[str] = mapped_column(String(8), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class InstrumentResearchRun(Base):
    __tablename__ = "instrument_research_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'complete', 'partial', 'failed')",
            name="ck_instrument_research_runs_status",
        ),
        CheckConstraint(
            "sources_attempted >= 0 AND sources_succeeded >= 0 "
            "AND sources_succeeded <= sources_attempted",
            name="ck_instrument_research_runs_source_counts",
        ),
        Index(
            "ix_instrument_research_runs_user_instrument_started",
            "user_id",
            "instrument_id",
            "started_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    instrument_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("instruments.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(12), nullable=False)
    sources_attempted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sources_succeeded: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failures: Mapped[list] = mapped_column(JSON_DOCUMENT, nullable=False, default=list)


class InstrumentResearchBrief(Base):
    __tablename__ = "instrument_research_briefs"
    __table_args__ = (
        UniqueConstraint("run_id", name="uq_instrument_research_briefs_run"),
        CheckConstraint("renderer IN ('template', 'ai')", name="ck_instrument_research_briefs_renderer"),
        Index(
            "ix_instrument_research_briefs_instrument_created",
            "instrument_id",
            "created_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("instrument_research_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    instrument_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("instruments.id", ondelete="CASCADE"), nullable=False
    )
    sections: Mapped[dict] = mapped_column(JSON_DOCUMENT, nullable=False)
    renderer: Mapped[str] = mapped_column(String(12), nullable=False)
    data_quality: Mapped[list] = mapped_column(JSON_DOCUMENT, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class InstrumentSourceLink(Base):
    __tablename__ = "instrument_source_links"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "instrument_id",
            "url",
            name="uq_instrument_source_links_user_instrument_url",
        ),
        Index(
            "ix_instrument_source_links_user_instrument",
            "user_id",
            "instrument_id",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    instrument_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("instruments.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PositionThesis(Base):
    __tablename__ = "position_theses"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'invalidated', 'closed')", name="ck_position_theses_status"
        ),
        CheckConstraint("conviction BETWEEN 1 AND 5", name="ck_position_theses_conviction"),
        Index(
            "uq_position_theses_active",
            "user_id",
            "portfolio_id",
            "instrument_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
            sqlite_where=text("status = 'active'"),
        ),
        Index("ix_position_theses_user_portfolio", "user_id", "portfolio_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    portfolio_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("portfolios.id", ondelete="CASCADE"), nullable=False
    )
    instrument_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("instruments.id", ondelete="CASCADE"), nullable=False
    )
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="active")
    entry_reason: Mapped[str] = mapped_column(Text, nullable=False)
    horizon_days: Mapped[int] = mapped_column(nullable=False)
    conviction: Mapped[int] = mapped_column(nullable=False)
    target_levels: Mapped[list] = mapped_column(JSON_DOCUMENT, nullable=False)
    stop_level: Mapped[Decimal | None] = mapped_column(Numeric(24, 8))
    invalidation_conditions: Mapped[list] = mapped_column(JSON_DOCUMENT, nullable=False)
    invalidation_notes: Mapped[str | None] = mapped_column(Text)
    max_loss_pct: Mapped[Decimal | None] = mapped_column(Numeric(7, 4))
    review_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PositionThesisRevision(Base):
    __tablename__ = "position_thesis_revisions"
    __table_args__ = (
        UniqueConstraint("thesis_id", "revision_number", name="uq_position_thesis_revisions_number"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    thesis_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("position_theses.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    revision_number: Mapped[int] = mapped_column(nullable=False)
    changed_fields: Mapped[list] = mapped_column(JSON_DOCUMENT, nullable=False)
    snapshot: Mapped[dict] = mapped_column(JSON_DOCUMENT, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
