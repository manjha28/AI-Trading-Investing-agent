"""Persist latest quotes for freshness and bounded provider caching.

Revision ID: 0008_market_quote_cache
Revises: 0007_positive_market_prices
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0008_market_quote_cache"
down_revision: Union[str, None] = "0007_positive_market_prices"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "market_quotes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("instrument_id", sa.Uuid(), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("open", sa.Numeric(24, 8), nullable=False),
        sa.Column("high", sa.Numeric(24, 8), nullable=False),
        sa.Column("low", sa.Numeric(24, 8), nullable=False),
        sa.Column("close", sa.Numeric(24, 8), nullable=False),
        sa.Column("volume", sa.Numeric(30, 8), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_synthetic", sa.Boolean(), nullable=False),
        sa.CheckConstraint(
            "low <= high AND low <= open AND low <= close "
            "AND high >= open AND high >= close",
            name="ck_market_quotes_ohlc_relationship",
        ),
        sa.CheckConstraint(
            "open > 0 AND high > 0 AND low > 0 AND close > 0",
            name="ck_market_quotes_positive_prices",
        ),
        sa.CheckConstraint("volume >= 0", name="ck_market_quotes_nonnegative_volume"),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "instrument_id",
            "source",
            name="uq_market_quotes_instrument_source",
        ),
    )
    op.create_index(
        "ix_market_quotes_instrument_retrieved",
        "market_quotes",
        ["instrument_id", "retrieved_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_market_quotes_instrument_retrieved", table_name="market_quotes")
    op.drop_table("market_quotes")
