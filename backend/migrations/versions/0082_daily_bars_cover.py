"""Covering index for the Market watch daily-bar read: only the daily rows, with the columns the table shows, so it never has to visit the large bar table.

Revision ID: 0082_daily_bars_cover
Revises: 0081_dhan_daily_realised
"""
from alembic import op
import sqlalchemy as sa

revision = "0082_daily_bars_cover"
down_revision = "0081_dhan_daily_realised"
branch_labels = None
depends_on = None

NAME = "ix_market_bars_daily_cover"


def upgrade() -> None:
    # CONCURRENTLY: price writes keep running while it builds (cannot run inside a transaction)
    with op.get_context().autocommit_block():
        op.create_index(NAME, "market_bars", ["instrument_id", "timestamp"], postgresql_where=sa.text("interval = '1d'"),
                        postgresql_include=["source", "high", "low", "close", "volume", "is_synthetic"], postgresql_concurrently=True, if_not_exists=True)


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(NAME, table_name="market_bars", postgresql_concurrently=True, if_exists=True)
