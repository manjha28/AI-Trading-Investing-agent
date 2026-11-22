"""Nightly Dhan-vs-bhavcopy close cross-check flags.

Revision ID: 0065_price_crosscheck
Revises: 0064_pipeline_v2_shadow

Additive: one new table; nothing existing is changed.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0065_price_crosscheck"
down_revision: Union[str, None] = "0064_pipeline_v2_shadow"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "price_crosscheck",
        sa.Column("id", sa.Uuid(), nullable=False), sa.Column("instrument_id", sa.Uuid(), nullable=False), sa.Column("session_date", sa.Date(), nullable=False),
        sa.Column("dhan_close", sa.Float(), nullable=False), sa.Column("reference_close", sa.Float(), nullable=False), sa.Column("deviation", sa.Float(), nullable=False),
        sa.Column("reference_source", sa.String(32), nullable=False), sa.Column("status", sa.String(16), nullable=False),
        sa.Column("first_flagged_at", sa.DateTime(timezone=True), nullable=False), sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"], ondelete="CASCADE"), sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("instrument_id", "session_date", "reference_source", name="uq_price_crosscheck_instrument_session"),
    )
    op.create_index("ix_price_crosscheck_status", "price_crosscheck", ["status"])


def downgrade() -> None:
    op.drop_index("ix_price_crosscheck_status", table_name="price_crosscheck")
    op.drop_table("price_crosscheck")
