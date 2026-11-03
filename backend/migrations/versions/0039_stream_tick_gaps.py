"""Persist raw Dhan packets and explicit market feed gaps.

Revision ID: 0039_stream_tick_gaps
Revises: 0038_dhan_trade_strategy_setup
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0039_stream_tick_gaps"
down_revision: Union[str, None] = "0038_dhan_trade_strategy_setup"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, None] = None


def upgrade() -> None:
    op.create_table(
        "dhan_market_ticks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("instrument_id", sa.Uuid(), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("open", sa.Numeric(24, 8), nullable=False),
        sa.Column("high", sa.Numeric(24, 8), nullable=False),
        sa.Column("low", sa.Numeric(24, 8), nullable=False),
        sa.Column("close", sa.Numeric(24, 8), nullable=False),
        sa.Column("volume", sa.Numeric(30, 8), nullable=False),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_dhan_market_ticks_instrument_timestamp",
        "dhan_market_ticks",
        ["instrument_id", "timestamp"],
    )
    op.create_table(
        "market_data_gaps",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("instrument_id", sa.Uuid(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reason", sa.String(200), nullable=False),
        sa.Column("backfilled_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_market_data_gaps_instrument_window",
        "market_data_gaps",
        ["instrument_id", "started_at", "ended_at"],
    )
    op.create_table(
        "dhan_ledger_data_quality_exceptions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("entry_date", sa.String(40), nullable=True),
        sa.Column("description", sa.String(1000), nullable=True),
        sa.Column("debit", sa.Numeric(24, 2), nullable=True),
        sa.Column("credit", sa.Numeric(24, 2), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("occurrence_count", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "fingerprint", name="uq_dhan_ledger_quality_user_fingerprint"),
    )


def downgrade() -> None:
    op.drop_table("dhan_ledger_data_quality_exceptions")
    op.drop_index("ix_market_data_gaps_instrument_window", table_name="market_data_gaps")
    op.drop_table("market_data_gaps")
    op.drop_index("ix_dhan_market_ticks_instrument_timestamp", table_name="dhan_market_ticks")
    op.drop_table("dhan_market_ticks")
