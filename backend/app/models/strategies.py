from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    Text,
    event,
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
    # Intraday workspace (NULL on legacy daily versions, which are left exactly as stored):
    schema_version: Mapped[str | None] = mapped_column(String(40))
    source_type: Mapped[str | None] = mapped_column(String(16))
    definition: Mapped[dict | None] = mapped_column(JSON_DOCUMENT)
    definition_hash: Mapped[str | None] = mapped_column(String(64))
    source_artifact: Mapped[dict | None] = mapped_column(JSON_DOCUMENT)  # original Pine/Python text + filename + sha256
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # set when a backtest first references this version


@event.listens_for(StrategyVersion, "before_update")
def _versions_are_immutable(mapper, connection, target) -> None:  # noqa: ANN001
    """A saved version's parameters/definition never change; edits create a new version."""
    from sqlalchemy import inspect as sa_inspect
    state = sa_inspect(target)
    for column in ("params", "definition", "definition_hash", "source_artifact", "schema_version", "source_type"):
        if state.attrs[column].history.has_changes():
            raise ValueError(f"strategy versions are immutable; cannot change {column}")


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


class BacktestRun(Base):
    __tablename__ = "backtest_runs"
    __table_args__ = (
        CheckConstraint("status IN ('queued', 'running', 'completed', 'failed')", name="ck_backtest_runs_status"),
        Index("ix_backtest_runs_user_created", "user_id", "created_at"),
        Index("ix_backtest_runs_strategy", "strategy_id", "strategy_version"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    strategy_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("strategies.id", ondelete="CASCADE"), nullable=False)
    strategy_version: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False)
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    config: Mapped[dict] = mapped_column(JSON_DOCUMENT, nullable=False)
    definition_snapshot: Mapped[dict] = mapped_column(JSON_DOCUMENT, nullable=False)
    definition_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    provenance: Mapped[dict | None] = mapped_column(JSON_DOCUMENT)
    result: Mapped[dict | None] = mapped_column(JSON_DOCUMENT)
    error: Mapped[str | None] = mapped_column(Text)
    engine_version: Mapped[str] = mapped_column(String(40), nullable=False)
    indicator_version: Mapped[str] = mapped_column(String(40), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class IntradaySignal(Base):
    __tablename__ = "intraday_signals"
    __table_args__ = (
        CheckConstraint("direction IN ('long', 'short')", name="ck_intraday_signals_direction"),
        CheckConstraint("status IN ('forming', 'ready', 'active', 'partially_realised', 'target', 'stopped', 'invalidated', 'expired', 'cancelled')",
                        name="ck_intraday_signals_status"),
        UniqueConstraint("strategy_id", "dedupe_key", name="uq_intraday_signals_strategy_dedupe"),
        Index("ix_intraday_signals_user_created", "user_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    strategy_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("strategies.id", ondelete="CASCADE"), nullable=False)
    strategy_version: Mapped[int] = mapped_column(Integer, nullable=False)
    instrument_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("instruments.id", ondelete="CASCADE"), nullable=False)
    market: Mapped[str] = mapped_column(String(12), nullable=False)
    direction: Mapped[str] = mapped_column(String(8), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)  # CURRENT lifecycle state; changed only via services.signal_lifecycle (history: IntradaySignalEvent)
    suppression_reason: Mapped[str | None] = mapped_column(String(120))  # legacy; suppressed candidates are now ScanRejection rows
    timeframe: Mapped[str] = mapped_column(String(8), nullable=False)
    signal_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)  # close of the decision candle
    reference_price: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    indicators: Mapped[dict] = mapped_column(JSON_DOCUMENT, nullable=False)
    conditions_met: Mapped[list] = mapped_column(JSON_DOCUMENT, nullable=False)
    conditions_unmet: Mapped[list] = mapped_column(JSON_DOCUMENT, nullable=False)
    rationale: Mapped[str] = mapped_column(String(500), nullable=False)
    data_freshness: Mapped[dict] = mapped_column(JSON_DOCUMENT, nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(200), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    option: Mapped[dict | None] = mapped_column(JSON_DOCUMENT)   # selected contract / NO TRADE reasons for options strategies


class OptionContract(Base):
    """Effective-dated option contract master row. Lot size, strike and expiry come from the ingested master, never from code."""
    __tablename__ = "option_contracts"
    __table_args__ = (
        CheckConstraint("option_type IN ('CE', 'PE')", name="ck_option_contracts_type"),
        UniqueConstraint("exchange", "segment", "security_id", "valid_from", name="uq_option_contracts_security"),
        Index("ix_option_contracts_lookup", "underlying", "expiry", "valid_from"),
        Index("ix_option_contracts_status", "status", "exchange", "segment", "underlying"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    underlying: Mapped[str] = mapped_column(String(32), nullable=False)          # e.g. NIFTY
    trading_symbol: Mapped[str] = mapped_column(String(80), nullable=False)
    provider_id: Mapped[str | None] = mapped_column(String(64))                  # e.g. Dhan SECURITY_ID|SEGMENT
    expiry: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    strike: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    option_type: Mapped[str] = mapped_column(String(2), nullable=False)
    lot_size: Mapped[int | None] = mapped_column(Integer)                        # NULL = unknown: the selector refuses such a contract
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    instrument_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("instruments.id", ondelete="SET NULL"))  # where 1m option candles live
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    master_version: Mapped[str] = mapped_column(String(64), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # 0051 (additive): full Dhan identity so one table serves every exchange/segment
    exchange: Mapped[str] = mapped_column(String(8), nullable=False, default="NSE")
    segment: Mapped[str] = mapped_column(String(16), nullable=False, default="NSE_FNO")
    security_id: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    instrument_type: Mapped[str | None] = mapped_column(String(8))
    underlying_security_id: Mapped[str | None] = mapped_column(String(32))
    tick_size: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="active")
    expiry_flag: Mapped[str | None] = mapped_column(String(2))
    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    spec_hash: Mapped[str | None] = mapped_column(String(40))
    derivative_instrument_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("derivative_instruments.id", ondelete="SET NULL"))


class OptionQuote(Base):
    """Timestamped option quote (and provider Greeks/IV). ``quote_time`` is the PROVIDER observation time; ``ingested_at`` is when we stored it."""
    __tablename__ = "option_quotes"
    __table_args__ = (
        CheckConstraint("greeks_source IS NULL OR greeks_source IN ('provider', 'calculated')", name="ck_option_quotes_greeks_source"),
        UniqueConstraint("contract_id", "quote_time", "source", name="uq_option_quotes_contract_time_source"),
        Index("ix_option_quotes_time", "contract_id", "quote_time"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    contract_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("option_contracts.id", ondelete="CASCADE"), nullable=False)
    quote_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    bid: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    ask: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    bid_qty: Mapped[int | None] = mapped_column(Integer)
    ask_qty: Mapped[int | None] = mapped_column(Integer)
    ltp: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    volume: Mapped[int | None] = mapped_column(Integer)
    open_interest: Mapped[int | None] = mapped_column(Integer)
    implied_volatility: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    delta: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    greeks_source: Mapped[str | None] = mapped_column(String(12))
    underlying_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))   # underlying level the provider reported with this quote
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    # 0051 (additive)
    snapshot_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("option_chain_snapshots.id", ondelete="SET NULL"))
    gamma: Mapped[Decimal | None] = mapped_column(Numeric(14, 8))
    theta: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    vega: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    previous_open_interest: Mapped[int | None] = mapped_column(Integer)
    previous_volume: Mapped[int | None] = mapped_column(Integer)


# ───────────────────────── append-only signal lifecycle history + scan rejections ─────────────────────────
SIGNAL_STATES = ("forming", "ready", "active", "partially_realised", "target", "stopped", "invalidated", "expired", "cancelled")
# Reason codes are enumerated (CHECK constraint below); add a code with a migration, never free text.
EVENT_REASON_CODES = ("signal_created", "confirmed_next_scan", "exit_condition", "opposite_signal", "time_expired", "owner_cancel",
                      "target_reached", "stop_hit", "partial_realisation", "conditions_complete", "backfill_genesis")
REJECTION_REASON_CODES = ("insufficient_bars", "stale_data", "synthetic_data", "missing_data_limit", "stale_spot", "no_history", "other")


def _quoted(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


class IntradaySignalEvent(Base):
    """INSERT-only record of one signal state transition. ``event_time`` is when the system recorded it (processing time);
    ``observed_data_time`` is the market timestamp of the data that caused it (data time). They are separate, never defaulted to each other.
    ``observed_data_time`` is NULL only for rows backfilled by the migration, where the original data time is unknown and is not invented."""
    __tablename__ = "intraday_signal_events"
    __table_args__ = (
        CheckConstraint(f"to_state IN ({_quoted(SIGNAL_STATES)})", name="ck_intraday_signal_events_to_state"),
        CheckConstraint(f"from_state IS NULL OR from_state IN ({_quoted(SIGNAL_STATES)})", name="ck_intraday_signal_events_from_state"),
        CheckConstraint(f"reason_code IN ({_quoted(EVENT_REASON_CODES)})", name="ck_intraday_signal_events_reason"),
        CheckConstraint("observed_data_time IS NOT NULL OR source = 'migration_backfill'", name="ck_intraday_signal_events_data_time"),
        Index("ix_intraday_signal_events_signal", "signal_id", "event_time"),
        Index("ix_intraday_signal_events_user_time", "user_id", "event_time"),
    )

    event_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    signal_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("intraday_signals.id", ondelete="CASCADE"), nullable=False)
    from_state: Mapped[str | None] = mapped_column(String(24))  # NULL only for the creation event
    to_state: Mapped[str] = mapped_column(String(24), nullable=False)
    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    observed_data_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reason_code: Mapped[str] = mapped_column(String(32), nullable=False)
    reason_detail: Mapped[str | None] = mapped_column(String(500))
    strategy_version_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("strategy_versions.id", ondelete="CASCADE"), nullable=False)
    source: Mapped[str] = mapped_column(String(80), nullable=False)  # e.g. "scan:<run id>", "lazy_expiry", "manual", "migration_backfill"
    payload: Mapped[dict] = mapped_column(JSON_DOCUMENT, nullable=False)


class ScanRejection(Base):
    """A scan candidate that never became a signal (so it is not a lifecycle state). INSERT-only."""
    __tablename__ = "scan_rejections"
    __table_args__ = (
        CheckConstraint(f"reason_code IN ({_quoted(REJECTION_REASON_CODES)})", name="ck_scan_rejections_reason"),
        CheckConstraint("direction IS NULL OR direction IN ('long', 'short')", name="ck_scan_rejections_direction"),
        Index("ix_scan_rejections_run", "user_id", "scan_run_id"),
        Index("ix_scan_rejections_user_time", "user_id", "recorded_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    scan_run_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))  # NULL only for rows migrated from legacy "suppressed" signals
    strategy_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("strategies.id", ondelete="CASCADE"), nullable=False)
    strategy_version_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("strategy_versions.id", ondelete="CASCADE"), nullable=False)
    instrument_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("instruments.id", ondelete="CASCADE"), nullable=False)
    direction: Mapped[str | None] = mapped_column(String(8))  # NULL when no candidate existed (e.g. no history)
    reason_code: Mapped[str] = mapped_column(String(32), nullable=False)
    reason_detail: Mapped[str | None] = mapped_column(String(500))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)  # processing time
    observed_data_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # data time; NULL when no usable bars existed
    payload: Mapped[dict] = mapped_column(JSON_DOCUMENT, nullable=False)


def _append_only(mapper, connection, target) -> None:  # noqa: ANN001
    raise ValueError(f"{target.__tablename__} is append-only; rows cannot be updated or deleted")


for _model in (IntradaySignalEvent, ScanRejection):
    event.listen(_model, "before_update", _append_only)
    event.listen(_model, "before_delete", _append_only)


@event.listens_for(IntradaySignal, "before_update")
def _status_changes_only_via_lifecycle_service(mapper, connection, target) -> None:  # noqa: ANN001
    """Signal state is written in exactly one place: services.signal_lifecycle.transition_signal."""
    from sqlalchemy import inspect as sa_inspect
    state = sa_inspect(target)
    if state.attrs.status.history.has_changes():
        if not state.info.pop("lifecycle_write", False):
            raise ValueError("signal status may only be changed through services.signal_lifecycle.transition_signal")
