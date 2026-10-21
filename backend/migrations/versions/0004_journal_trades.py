"""Add user-owned journal trades.

Revision ID: 0004_journal_trades
Revises: 0003_portfolio_transactions
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0004_journal_trades"
down_revision: Union[str, None] = "0003_portfolio_transactions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "journal_trades",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("asset_class", sa.String(length=12), nullable=False),
        sa.Column("instrument", sa.String(length=64), nullable=False),
        sa.Column("side", sa.String(length=8), nullable=False),
        sa.Column("quantity", sa.Numeric(24, 8), nullable=False),
        sa.Column("multiplier", sa.Numeric(18, 8), nullable=False),
        sa.Column("entry_price", sa.Numeric(24, 8), nullable=False),
        sa.Column("exit_price", sa.Numeric(24, 8), nullable=True),
        sa.Column("entry_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("exit_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fees", sa.Numeric(24, 2), nullable=False),
        sa.Column("planned_risk", sa.Numeric(24, 2), nullable=True),
        sa.Column("strategy", sa.String(length=100), nullable=True),
        sa.Column("setup", sa.String(length=100), nullable=True),
        sa.Column("notes", sa.String(length=4000), nullable=True),
        sa.Column("tags", sa.JSON(), nullable=False),
        sa.Column("option_type", sa.String(length=4), nullable=True),
        sa.Column("strike", sa.Numeric(24, 8), nullable=True),
        sa.Column("expiry", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("asset_class IN ('equity', 'future', 'option', 'crypto')", name="ck_journal_asset_class"),
        sa.CheckConstraint("side IN ('long', 'short')", name="ck_journal_side"),
        sa.CheckConstraint("quantity > 0 AND multiplier > 0 AND entry_price > 0", name="ck_journal_trade_positive_values"),
        sa.CheckConstraint("fees >= 0", name="ck_journal_trade_fees"),
    )
    op.create_index("ix_journal_trades_user_exit_time", "journal_trades", ["user_id", "exit_time"])


def downgrade() -> None:
    op.drop_index("ix_journal_trades_user_exit_time", table_name="journal_trades")
    op.drop_table("journal_trades")