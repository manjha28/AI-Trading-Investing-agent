"""Delta live-execution state and order ledger, and the per-test execute_live flag (all off by default).

Revision ID: 0074_delta_live_execution
Revises: 0073_crypto_forward_tests

Additive: two new tables and one new column with a false default; nothing existing is changed.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.models import JSON_DOCUMENT

revision: str = "0074_delta_live_execution"
down_revision: Union[str, None] = "0073_crypto_forward_tests"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("crypto_forward_tests", sa.Column("execute_live", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_table(
        "delta_live_state",
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True), sa.Column("armed", sa.Boolean(), nullable=False),
        sa.Column("armed_at", sa.DateTime(timezone=True)), sa.Column("disarmed_at", sa.DateTime(timezone=True)),
        sa.Column("max_daily_loss_usd", sa.Numeric(18, 2), nullable=False), sa.Column("max_trades_per_day", sa.Integer(), nullable=False),
        sa.Column("max_open_positions", sa.Integer(), nullable=False), sa.Column("max_notional_usd", sa.Numeric(18, 2), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "delta_live_orders",
        sa.Column("id", sa.Uuid(), primary_key=True), sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("forward_test_id", sa.Uuid(), sa.ForeignKey("crypto_forward_tests.id", ondelete="SET NULL")),
        sa.Column("strategy_key", sa.String(64), nullable=False), sa.Column("symbol", sa.String(32), nullable=False), sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("side", sa.String(4), nullable=False), sa.Column("contracts", sa.Integer(), nullable=False), sa.Column("leverage", sa.Integer(), nullable=False),
        sa.Column("signal_time", sa.DateTime(timezone=True), nullable=False), sa.Column("reference_price", sa.Numeric(28, 10), nullable=False),
        sa.Column("stop_price", sa.Numeric(28, 10)), sa.Column("target_price", sa.Numeric(28, 10)), sa.Column("client_order_id", sa.String(30), nullable=False),
        sa.Column("delta_order_id", sa.String(40)), sa.Column("status", sa.String(24), nullable=False), sa.Column("block_reason", sa.String(300)),
        sa.Column("entry_price", sa.Numeric(28, 10)), sa.Column("request", JSON_DOCUMENT), sa.Column("response", JSON_DOCUMENT),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("client_order_id", name="uq_delta_live_orders_client_id"),
    )
    op.create_index("ix_delta_live_orders_user_created", "delta_live_orders", ["user_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_delta_live_orders_user_created", table_name="delta_live_orders")
    op.drop_table("delta_live_orders")
    op.drop_table("delta_live_state")
    op.drop_column("crypto_forward_tests", "execute_live")
