"""Crypto (Delta Exchange) research: completed candles and stored backtest runs.

Revision ID: 0059_crypto_delta
Revises: 0058_execution_audit

Additive: two new tables, separate from the Indian-equity market_bars and backtest tables.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0059_crypto_delta"
down_revision: Union[str, None] = "0058_execution_audit"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSON_DOC = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "crypto_candles",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("resolution", sa.String(4), nullable=False),
        sa.Column("open_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("open", sa.Numeric(28, 10), nullable=False),
        sa.Column("high", sa.Numeric(28, 10), nullable=False),
        sa.Column("low", sa.Numeric(28, 10), nullable=False),
        sa.Column("close", sa.Numeric(28, 10), nullable=False),
        sa.Column("volume", sa.Numeric(28, 10), nullable=False),
        sa.Column("source", sa.String(40), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("symbol", "resolution", "open_time", name="uq_crypto_candles_key"),
    )
    op.create_index("ix_crypto_candles_lookup", "crypto_candles", ["symbol", "resolution", "open_time"])
    op.create_table(
        "crypto_backtest_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("strategy_id", sa.String(32), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("resolution", sa.String(4), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("bars_used", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("hypothetical", sa.Boolean(), nullable=False),
        sa.Column("params", JSON_DOC, nullable=False),
        sa.Column("assumptions", JSON_DOC, nullable=False),
        sa.Column("data_quality", JSON_DOC, nullable=False),
        sa.Column("metrics", JSON_DOC, nullable=False),
        sa.Column("trades", JSON_DOC, nullable=False),
        sa.Column("equity", JSON_DOC, nullable=False),
    )
    op.create_index("ix_crypto_backtest_user_created", "crypto_backtest_runs", ["user_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_crypto_backtest_user_created", table_name="crypto_backtest_runs")
    op.drop_table("crypto_backtest_runs")
    op.drop_index("ix_crypto_candles_lookup", table_name="crypto_candles")
    op.drop_table("crypto_candles")
