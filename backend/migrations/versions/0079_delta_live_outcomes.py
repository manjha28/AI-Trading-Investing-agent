"""Outcome of each live Delta order (target / stop / manual / liquidated / open / cancelled), read from Delta.

Revision ID: 0079_delta_live_outcomes
Revises: 0078_delta_scanners

Additive: one new table; nothing existing is changed.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.models import JSON_DOCUMENT

revision: str = "0079_delta_live_outcomes"
down_revision: Union[str, None] = "0078_delta_scanners"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "delta_live_outcomes",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("order_id", sa.Uuid(), sa.ForeignKey("delta_live_orders.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("outcome", sa.String(24), nullable=False),
        sa.Column("entry_price", sa.Numeric(28, 10)), sa.Column("entry_at", sa.DateTime(timezone=True)),
        sa.Column("exit_price", sa.Numeric(28, 10)), sa.Column("exit_at", sa.DateTime(timezone=True)),
        sa.Column("realized_pnl", sa.Numeric(28, 10)), sa.Column("fees", sa.Numeric(28, 10)), sa.Column("funding", sa.Numeric(28, 10)), sa.Column("net_pnl", sa.Numeric(28, 10)),
        sa.Column("detail", JSON_DOCUMENT), sa.Column("synced_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("order_id", name="uq_delta_live_outcomes_order"),
    )
    op.create_index("ix_delta_live_outcomes_user_id", "delta_live_outcomes", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_delta_live_outcomes_user_id", table_name="delta_live_outcomes")
    op.drop_table("delta_live_outcomes")
