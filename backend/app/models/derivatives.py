"""Dhan derivatives data platform tables. Option CONTRACTS and QUOTES live in the 0050 tables (``option_contracts`` / ``option_quotes``, extended
additively); futures contracts, candles, rolling-strike series, chain snapshots, coverage and ingestion jobs are here. Nothing duplicates 0050."""
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import BigInteger, Boolean, CheckConstraint, Date, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models import JSON_DOCUMENT


class DerivativeInstrument(Base):
    """An underlying that has listed derivatives (index, stock, commodity, currency), discovered from the instrument master."""
    __tablename__ = "derivative_instruments"
    __table_args__ = (UniqueConstraint("exchange", "master_segment", "underlying_symbol", name="uq_derivative_instruments_identity"),
                      CheckConstraint("category IN ('index', 'stock', 'commodity', 'currency')", name="ck_derivative_instruments_category"))

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    exchange: Mapped[str] = mapped_column(String(8), nullable=False)
    master_segment: Mapped[str] = mapped_column(String(4), nullable=False)          # D / M / C as in the master
    segment: Mapped[str] = mapped_column(String(16), nullable=False)                # Dhan API segment, e.g. NSE_FNO
    category: Mapped[str] = mapped_column(String(12), nullable=False)
    underlying_symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    underlying_security_id: Mapped[str | None] = mapped_column(String(32))          # the master's UNDERLYING_SECURITY_ID (F&O numbering)
    cash_security_id: Mapped[str | None] = mapped_column(String(32))                # index/equity security the chain & rolling APIs call the underlying
    cash_segment: Mapped[str | None] = mapped_column(String(16))
    display_name: Mapped[str | None] = mapped_column(String(160))
    has_futures: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    has_options: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="active")
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="dhan_master")


class DerivativeContract(Base):
    """Non-option derivative contracts (futures). Specification history is preserved: a changed spec closes the old row (valid_to) and opens a new one."""
    __tablename__ = "derivative_contracts"
    __table_args__ = (UniqueConstraint("exchange", "segment", "security_id", "valid_from", name="uq_derivative_contracts_identity"),
                      Index("ix_derivative_contracts_lookup", "underlying_symbol", "instrument_type", "expiry"),
                      Index("ix_derivative_contracts_status", "status", "exchange", "segment"))

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    derivative_instrument_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("derivative_instruments.id", ondelete="SET NULL"))
    exchange: Mapped[str] = mapped_column(String(8), nullable=False)
    segment: Mapped[str] = mapped_column(String(16), nullable=False)
    master_segment: Mapped[str] = mapped_column(String(4), nullable=False)
    instrument_type: Mapped[str] = mapped_column(String(8), nullable=False)         # FUTIDX, FUTSTK, FUTCOM, FUTCUR
    security_id: Mapped[str] = mapped_column(String(32), nullable=False)
    underlying_symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    underlying_security_id: Mapped[str | None] = mapped_column(String(32))
    trading_symbol: Mapped[str] = mapped_column(String(96), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(160))
    expiry: Mapped[date] = mapped_column(Date, nullable=False)
    expiry_flag: Mapped[str | None] = mapped_column(String(2))
    lot_size: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    tick_size: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="active")      # active | expired | removed
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    spec_hash: Mapped[str] = mapped_column(String(40), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="dhan_master")
    master_version: Mapped[str] = mapped_column(String(64), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DerivativeMasterSnapshot(Base):
    __tablename__ = "derivative_master_snapshots"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_url: Mapped[str] = mapped_column(String(300), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    rows_total: Mapped[int] = mapped_column(Integer, nullable=False)
    derivative_rows: Mapped[int] = mapped_column(Integer, nullable=False)
    new_contracts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    changed_contracts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    expired_contracts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    removed_contracts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    detail: Mapped[dict | None] = mapped_column(JSON_DOCUMENT)


class FuturesCandle1m(Base):
    __tablename__ = "futures_candles_1m"
    __table_args__ = (Index("ix_futures_candles_1m_time", "ts"),)

    contract_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("derivative_contracts.id", ondelete="CASCADE"), primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)        # candle OPEN time, UTC (Dhan epoch)
    open: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    high: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    low: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    close: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    volume: Mapped[int] = mapped_column(BigInteger, nullable=False)
    open_interest: Mapped[int | None] = mapped_column(BigInteger)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="dhan_intraday")
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class OptionCandle1m(Base):
    """1-minute candles of ACTIVE option contracts (Dhan /v2/charts/intraday) keyed by the exact contract. Not rolling-strike data."""
    __tablename__ = "options_candles_1m"
    __table_args__ = (Index("ix_options_candles_1m_time", "ts"),)

    contract_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("option_contracts.id", ondelete="CASCADE"), primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    open: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    high: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    low: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    close: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    volume: Mapped[int] = mapped_column(BigInteger, nullable=False)
    open_interest: Mapped[int | None] = mapped_column(BigInteger)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="dhan_intraday")
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RollingOptionCandle1m(Base):
    """Expired-options history from Dhan /v2/charts/rollingoption. ATM-relative: ``strike_offset`` ('ATM+1') is NOT a fixed contract. ``strike`` is the absolute
    strike the provider reported for that candle; the series is only ever labelled as rolling-strike data."""
    __tablename__ = "rolling_options_candles_1m"
    __table_args__ = (UniqueConstraint("exchange", "segment", "underlying_security_id", "instrument_type", "expiry_flag", "expiry_code", "strike_offset", "option_type", "ts",
                                       name="uq_rolling_options_candles_identity"),
                      Index("ix_rolling_options_candles_series", "underlying_symbol", "expiry_flag", "expiry_code", "strike_offset", "option_type", "ts"))

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    exchange: Mapped[str] = mapped_column(String(8), nullable=False)
    segment: Mapped[str] = mapped_column(String(16), nullable=False)
    underlying_security_id: Mapped[str] = mapped_column(String(32), nullable=False)    # the cash/index security id used in the request
    underlying_symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    instrument_type: Mapped[str] = mapped_column(String(8), nullable=False)
    expiry_flag: Mapped[str] = mapped_column(String(8), nullable=False)               # WEEK | MONTH
    expiry_code: Mapped[int] = mapped_column(Integer, nullable=False)
    strike_offset: Mapped[str] = mapped_column(String(12), nullable=False)
    option_type: Mapped[str] = mapped_column(String(2), nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    open: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    high: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    low: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    close: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    volume: Mapped[int | None] = mapped_column(BigInteger)
    open_interest: Mapped[int | None] = mapped_column(BigInteger)
    implied_volatility: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    spot: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    strike: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="dhan_rollingoption")
    request: Mapped[dict | None] = mapped_column(JSON_DOCUMENT)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class FuturesCandle1d(Base):
    """Daily futures candles. ``series_kind`` 'contract' = one exact contract; 'provider_rolled' = Dhan's expiryCode series whose rollover is NOT documented
    (never spliced or adjusted by us)."""
    __tablename__ = "futures_candles_1d"
    __table_args__ = (UniqueConstraint("exchange", "segment", "security_id", "series_kind", "expiry_code", "day", name="uq_futures_candles_1d_identity"),)

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    contract_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("derivative_contracts.id", ondelete="SET NULL"))
    exchange: Mapped[str] = mapped_column(String(8), nullable=False)
    segment: Mapped[str] = mapped_column(String(16), nullable=False)
    security_id: Mapped[str] = mapped_column(String(32), nullable=False)
    series_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    expiry_code: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    day: Mapped[date] = mapped_column(Date, nullable=False)
    open: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    high: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    low: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    close: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    volume: Mapped[int] = mapped_column(BigInteger, nullable=False)
    open_interest: Mapped[int | None] = mapped_column(BigInteger)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="dhan_historical")
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class OptionChainSnapshot(Base):
    """One live /v2/optionchain response (a set of option_quotes rows share its id). Provider timestamp is not supplied by the endpoint: ``captured_at`` is ours."""
    __tablename__ = "option_chain_snapshots"
    __table_args__ = (Index("ix_option_chain_snapshots_lookup", "exchange", "underlying_symbol", "expiry", "captured_at"),)

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    derivative_instrument_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("derivative_instruments.id", ondelete="SET NULL"))
    exchange: Mapped[str] = mapped_column(String(8), nullable=False)
    underlying_symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    underlying_security_id: Mapped[str] = mapped_column(String(32), nullable=False)
    underlying_segment: Mapped[str] = mapped_column(String(16), nullable=False)
    expiry: Mapped[date] = mapped_column(Date, nullable=False)
    underlying_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    strikes: Mapped[int] = mapped_column(Integer, nullable=False)
    quotes_stored: Mapped[int] = mapped_column(Integer, nullable=False)
    quotes_with_bid_ask: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="dhan_optionchain")


class DerivativeDataCoverage(Base):
    __tablename__ = "derivative_data_coverage"
    __table_args__ = (UniqueConstraint("scope_key", name="uq_derivative_data_coverage_scope"),)

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    scope_key: Mapped[str] = mapped_column(String(200), nullable=False)
    data_type: Mapped[str] = mapped_column(String(40), nullable=False)
    category: Mapped[str] = mapped_column(String(12), nullable=False)
    exchange: Mapped[str] = mapped_column(String(8), nullable=False)
    segment: Mapped[str] = mapped_column(String(16), nullable=False)
    instrument_type: Mapped[str] = mapped_column(String(8), nullable=False)
    underlying_symbol: Mapped[str | None] = mapped_column(String(64))
    timeframe: Mapped[str] = mapped_column(String(8), nullable=False)
    provenance: Mapped[str] = mapped_column(String(12), nullable=False)               # historical | live
    contract_state: Mapped[str] = mapped_column(String(12), nullable=False)            # active | expired | rolling | n/a
    instruments: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    contracts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    row_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    first_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sessions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    missing_intervals: Mapped[dict | None] = mapped_column(JSON_DOCUMENT)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_status: Mapped[str | None] = mapped_column(String(16))
    last_error: Mapped[str | None] = mapped_column(Text)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default="dhan")
    blocker: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DerivativeIngestionJob(Base):
    __tablename__ = "derivative_ingestion_jobs"
    __table_args__ = (CheckConstraint("status IN ('queued', 'running', 'succeeded', 'partial', 'failed', 'cancelled')", name="ck_derivative_jobs_status"),
                      Index("ix_derivative_jobs_status", "status", "priority", "created_at"))

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=50)
    params: Mapped[dict] = mapped_column(JSON_DOCUMENT, nullable=False)
    tasks_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tasks_done: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tasks_failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rows_written: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    user_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"))


class DerivativeIngestionTask(Base):
    __tablename__ = "derivative_ingestion_tasks"
    __table_args__ = (UniqueConstraint("job_id", "task_key", name="uq_derivative_tasks_job_key"), Index("ix_derivative_tasks_job_status", "job_id", "status"),
                      CheckConstraint("status IN ('pending', 'running', 'done', 'empty', 'failed', 'dead', 'cancelled')", name="ck_derivative_tasks_status"))

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    job_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("derivative_ingestion_jobs.id", ondelete="CASCADE"), nullable=False)
    task_key: Mapped[str] = mapped_column(String(240), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False)
    params: Mapped[dict] = mapped_column(JSON_DOCUMENT, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
