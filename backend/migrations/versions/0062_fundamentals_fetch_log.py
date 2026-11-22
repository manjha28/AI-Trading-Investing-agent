"""Append-only log of Yahoo statement-fetch attempts (refreshed / rate_limited / no_statements / failed).

Revision ID: 0062_fundamentals_fetch_log
Revises: 0061_forward_and_dhan_live

Additive: one new table; nothing existing is changed.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0062_fundamentals_fetch_log"
down_revision: Union[str, None] = "0061_forward_and_dhan_live"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "fundamentals_fetch_log",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("instrument_id", sa.Uuid(), nullable=False),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("annual_periods", sa.Integer(), nullable=True),
        sa.Column("quarterly_periods", sa.Integer(), nullable=True),
        sa.Column("detail", sa.String(length=300), nullable=True),
        sa.Column("origin", sa.String(length=24), nullable=False),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("outcome IN ('refreshed', 'rate_limited', 'no_statements', 'failed')", name="ck_fundamentals_fetch_log_outcome"),
    )
    op.create_index("ix_fundamentals_fetch_log_instrument_time", "fundamentals_fetch_log", ["instrument_id", "attempted_at"])


def downgrade() -> None:
    op.drop_index("ix_fundamentals_fetch_log_instrument_time", table_name="fundamentals_fetch_log")
    op.drop_table("fundamentals_fetch_log")
