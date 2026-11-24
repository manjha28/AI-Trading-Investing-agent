"""Delta multi-perpetual paper scanner (paper only: no execute_live column).

Revision ID: 0078_delta_scanners
Revises: 0077_mcx_last_snapshots

Additive: one new table; nothing existing is changed.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.models import JSON_DOCUMENT

revision: str = "0078_delta_scanners"
down_revision: Union[str, None] = "0077_mcx_last_snapshots"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "delta_scanners",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(80), nullable=False), sa.Column("strategy_key", sa.String(32), nullable=False), sa.Column("resolution", sa.String(4), nullable=False),
        sa.Column("symbols", JSON_DOCUMENT, nullable=False), sa.Column("params", JSON_DOCUMENT, nullable=False),
        sa.Column("wallet_usd", sa.Numeric(18, 2), nullable=False), sa.Column("risk_pct", sa.Numeric(8, 5), nullable=False), sa.Column("total_risk_cap_pct", sa.Numeric(8, 5), nullable=False),
        sa.Column("leverage_cap", sa.Numeric(8, 2), nullable=False), sa.Column("label", sa.String(40), nullable=False, server_default="paper"),
        sa.Column("start_time", sa.DateTime(timezone=True), nullable=False), sa.Column("status", sa.String(12), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_run_at", sa.DateTime(timezone=True)), sa.Column("last_error", sa.String(400)), sa.Column("snapshot", JSON_DOCUMENT),
    )
    op.create_index("ix_delta_scanners_user_id", "delta_scanners", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_delta_scanners_user_id", table_name="delta_scanners")
    op.drop_table("delta_scanners")
