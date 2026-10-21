"""Add an instrument master and provenance-aware historical bars.

Revision ID: 0006_instruments_market_data
Revises: 0005_journal_currency
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0006_instruments_market_data"
down_revision: Union[str, None] = "0005_journal_currency"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "instruments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=160), nullable=False),
        sa.Column("asset_class", sa.String(length=12), nullable=False),
        sa.Column("venue", sa.String(length=32), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("country", sa.String(length=2), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("multiplier", sa.Numeric(18, 8), nullable=True),
        sa.Column("expiry", sa.Date(), nullable=True),
        sa.Column("strike", sa.Numeric(24, 8), nullable=True),
        sa.Column("option_type", sa.String(length=4), nullable=True),
        sa.Column("is_synthetic", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "asset_class IN ('equity', 'etf', 'index', 'future', 'option', 'crypto')",
            name="ck_instruments_asset_class",
        ),
        sa.CheckConstraint("length(currency) = 3", name="ck_instruments_currency_length"),
        sa.CheckConstraint(
            "(asset_class = 'option' AND expiry IS NOT NULL AND strike IS NOT NULL "
            "AND option_type IN ('call', 'put')) OR "
            "(asset_class != 'option' AND strike IS NULL AND option_type IS NULL)",
            name="ck_instruments_option_metadata",
        ),
        sa.CheckConstraint(
            "(asset_class IN ('future', 'option') AND multiplier IS NOT NULL AND multiplier > 0) "
            "OR (asset_class NOT IN ('future', 'option') AND multiplier IS NULL)",
            name="ck_instruments_derivative_multiplier",
        ),
        sa.CheckConstraint(
            "asset_class != 'future' OR expiry IS NOT NULL",
            name="ck_instruments_future_expiry",
        ),
    )
    op.create_index("ix_instruments_symbol_venue", "instruments", ["symbol", "venue"])
    op.create_index("ix_instruments_name", "instruments", ["display_name"])

    op.create_table(
        "instrument_provider_identifiers",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("instrument_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("provider_instrument_id", sa.String(length=128), nullable=False),
        sa.Column("provider_symbol", sa.String(length=96), nullable=False),
        sa.Column("is_verified", sa.Boolean(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "provider", "provider_instrument_id", name="uq_provider_instrument_identifier"
        ),
    )
    op.create_index(
        "ix_instrument_provider_identifiers_instrument",
        "instrument_provider_identifiers",
        ["instrument_id"],
    )

    op.create_table(
        "market_bars",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("instrument_id", sa.Uuid(), nullable=False),
        sa.Column("interval", sa.String(length=8), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("open", sa.Numeric(24, 8), nullable=False),
        sa.Column("high", sa.Numeric(24, 8), nullable=False),
        sa.Column("low", sa.Numeric(24, 8), nullable=False),
        sa.Column("close", sa.Numeric(24, 8), nullable=False),
        sa.Column("volume", sa.Numeric(30, 8), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_synthetic", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "instrument_id",
            "interval",
            "timestamp",
            "source",
            name="uq_market_bars_instrument_interval_time_source",
        ),
        sa.CheckConstraint("interval IN ('1d', '1h', '15m')", name="ck_market_bars_interval"),
        sa.CheckConstraint(
            "low <= high AND low <= open AND low <= close "
            "AND high >= open AND high >= close",
            name="ck_market_bars_ohlc_relationship",
        ),
        sa.CheckConstraint("volume >= 0", name="ck_market_bars_nonnegative_volume"),
    )
    op.create_index(
        "ix_market_bars_instrument_interval_time",
        "market_bars",
        ["instrument_id", "interval", "timestamp"],
    )

    op.create_table(
        "market_data_syncs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("instrument_id", sa.Uuid(), nullable=False),
        sa.Column("interval", sa.String(length=8), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("succeeded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("rows_received", sa.Integer(), nullable=False),
        sa.Column("safe_error_code", sa.String(length=48), nullable=True),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("instrument_id", "interval", "source", name="uq_market_sync_instrument"),
        sa.CheckConstraint("interval IN ('1d', '1h', '15m')", name="ck_market_sync_interval"),
        sa.CheckConstraint("status IN ('success', 'unavailable')", name="ck_market_sync_status"),
    )
    op.create_index(
        "ix_market_sync_instrument_interval",
        "market_data_syncs",
        ["instrument_id", "interval"],
    )


def downgrade() -> None:
    op.drop_index("ix_market_sync_instrument_interval", table_name="market_data_syncs")
    op.drop_table("market_data_syncs")
    op.drop_index("ix_market_bars_instrument_interval_time", table_name="market_bars")
    op.drop_table("market_bars")
    op.drop_index(
        "ix_instrument_provider_identifiers_instrument",
        table_name="instrument_provider_identifiers",
    )
    op.drop_table("instrument_provider_identifiers")
    op.drop_index("ix_instruments_name", table_name="instruments")
    op.drop_index("ix_instruments_symbol_venue", table_name="instruments")
    op.drop_table("instruments")
