"""Per-scanner Delta live switch (owner-approved, 2026-11-25): delta_scanners.execute_live (off) and delta_live_orders.scanner_id.

Revision ID: 0080_delta_scanner_live
Revises: 0079_delta_live_outcomes

Additive: two new nullable/defaulted columns; nothing existing is changed.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0080_delta_scanner_live"
down_revision: Union[str, None] = "0079_delta_live_outcomes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("delta_scanners", sa.Column("execute_live", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("delta_live_orders", sa.Column("scanner_id", sa.Uuid(), sa.ForeignKey("delta_scanners.id", ondelete="SET NULL", name="fk_delta_live_orders_scanner_id"), nullable=True))


def downgrade() -> None:
    op.drop_constraint("fk_delta_live_orders_scanner_id", "delta_live_orders", type_="foreignkey")
    op.drop_column("delta_live_orders", "scanner_id")
    op.drop_column("delta_scanners", "execute_live")
