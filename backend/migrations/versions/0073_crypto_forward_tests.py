"""Paper forward tests of the 5 EMA strategy on Delta perpetuals.

Revision ID: 0073_crypto_forward_tests
Revises: 0072_delivery_percentages

Additive: one new table; nothing existing is changed.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.models import JSON_DOCUMENT

revision: str = "0073_crypto_forward_tests"
down_revision: Union[str, None] = "0072_delivery_percentages"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "crypto_forward_tests",
        sa.Column("id", sa.Uuid(), primary_key=True), sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(80), nullable=False), sa.Column("symbol", sa.String(32), nullable=False), sa.Column("resolution", sa.String(4), nullable=False),
        sa.Column("params", JSON_DOCUMENT, nullable=False), sa.Column("contracts", sa.Integer(), nullable=False), sa.Column("slippage_bps", sa.Numeric(10, 4), nullable=False),
        sa.Column("start_time", sa.DateTime(timezone=True), nullable=False), sa.Column("status", sa.String(12), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_run_at", sa.DateTime(timezone=True)), sa.Column("last_error", sa.String(400)), sa.Column("snapshot", JSON_DOCUMENT),
    )
    op.create_index("ix_crypto_forward_user_created", "crypto_forward_tests", ["user_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_crypto_forward_user_created", table_name="crypto_forward_tests")
    op.drop_table("crypto_forward_tests")
