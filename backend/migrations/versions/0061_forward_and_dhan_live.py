"""Forward tests (paper by default) and the Dhan live-execution state/ledger.

Revision ID: 0061_forward_and_dhan_live
Revises: 0060_market_watch_extra

Additive: three new tables. Live order placement stays off unless the deployment flag AND the per-user arming row AND the
per-test execute_live flag are all set (see AGENTS.md, owner-approved Dhan exception).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0061_forward_and_dhan_live"
down_revision: Union[str, None] = "0060_market_watch_extra"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSON_DOC = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "forward_tests",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("strategy_key", sa.String(64), nullable=False),
        sa.Column("definition", JSON_DOC, nullable=False),
        sa.Column("definition_hash", sa.String(64), nullable=False),
        sa.Column("instrument_ids", JSON_DOC, nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(12), nullable=False, server_default="active"),
        sa.Column("execute_live", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("slippage_bps", sa.Numeric(10, 4), nullable=False, server_default="2"),
        sa.Column("cost_profile", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_run_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text()),
        sa.Column("snapshot", JSON_DOC),
    )
    op.create_index("ix_forward_tests_user_created", "forward_tests", ["user_id", "created_at"])
    op.create_table(
        "dhan_live_state",
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("armed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("armed_at", sa.DateTime(timezone=True)),
        sa.Column("disarmed_at", sa.DateTime(timezone=True)),
        sa.Column("max_daily_loss_inr", sa.Numeric(18, 2), nullable=False, server_default="500"),
        sa.Column("max_trades_per_day", sa.Integer(), nullable=False, server_default="10"),
        sa.Column("max_open_positions", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("max_notional_inr", sa.Numeric(18, 2), nullable=False, server_default="50000"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "dhan_live_orders",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("forward_test_id", sa.Uuid(), sa.ForeignKey("forward_tests.id", ondelete="SET NULL")),
        sa.Column("strategy_key", sa.String(64), nullable=False),
        sa.Column("instrument_id", sa.Uuid(), sa.ForeignKey("instruments.id", ondelete="CASCADE"), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("security_id", sa.String(32), nullable=False),
        sa.Column("side", sa.String(4), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("signal_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reference_price", sa.Numeric(24, 8), nullable=False),
        sa.Column("stop_price", sa.Numeric(24, 8)),
        sa.Column("target_price", sa.Numeric(24, 8)),
        sa.Column("client_order_id", sa.String(30), nullable=False),
        sa.Column("dhan_order_id", sa.String(40)),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("block_reason", sa.String(200)),
        sa.Column("entry_price", sa.Numeric(24, 8)),
        sa.Column("exit_price", sa.Numeric(24, 8)),
        sa.Column("exit_reason", sa.String(40)),
        sa.Column("realized_pnl", sa.Numeric(18, 2)),
        sa.Column("request", JSON_DOC),
        sa.Column("response", JSON_DOC),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("client_order_id", name="uq_dhan_live_orders_client_id"),
    )
    op.create_index("ix_dhan_live_orders_user_created", "dhan_live_orders", ["user_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_dhan_live_orders_user_created", table_name="dhan_live_orders")
    op.drop_table("dhan_live_orders")
    op.drop_table("dhan_live_state")
    op.drop_index("ix_forward_tests_user_created", table_name="forward_tests")
    op.drop_table("forward_tests")
