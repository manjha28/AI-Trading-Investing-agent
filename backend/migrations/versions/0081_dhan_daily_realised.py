"""Dhan's realised profit per IST trading day, captured while Dhan still lists the day (the reconcile page needs it after the day ends).

Revision ID: 0081_dhan_daily_realised
Revises: 0080_delta_scanner_live

Additive: one new table; nothing existing is changed.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.models import JSON_DOCUMENT

revision: str = "0081_dhan_daily_realised"
down_revision: Union[str, None] = "0080_delta_scanner_live"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "dhan_daily_realised",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("realised", sa.Numeric(24, 8)),
        sa.Column("positions_count", sa.Integer(), nullable=False),
        sa.Column("by_contract", JSON_DOCUMENT, nullable=False),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "day", name="uq_dhan_daily_realised_user_day"),
    )
    op.create_index("ix_dhan_daily_realised_user_id", "dhan_daily_realised", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_dhan_daily_realised_user_id", table_name="dhan_daily_realised")
    op.drop_table("dhan_daily_realised")
