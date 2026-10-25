"""Mirror Dhan past trades and ledger entries.

Revision ID: 0028_dhan_transaction_mirror
Revises: 0027_dhan_account_mirror
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0028_dhan_transaction_mirror"
down_revision: Union[str, None] = "0027_dhan_account_mirror"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "dhan_trade_mirror",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("source", sa.String(length=32), server_default="dhan", nullable=False),
        sa.Column("trade_id", sa.String(length=64), nullable=False),
        sa.Column("order_id", sa.String(length=64), nullable=True),
        sa.Column("symbol", sa.String(length=64), nullable=True),
        sa.Column("security_id", sa.String(length=64), nullable=True),
        sa.Column("exchange_segment", sa.String(length=32), nullable=True),
        sa.Column("side", sa.String(length=8), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=24, scale=8), nullable=False),
        sa.Column("price", sa.Numeric(precision=24, scale=8), nullable=False),
        sa.Column("charges", sa.Numeric(precision=24, scale=8), nullable=True),
        sa.Column("currency", sa.String(length=3), server_default="INR", nullable=False),
        sa.Column("trade_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id", "source", "trade_id",
            name="uq_dhan_trade_mirror_user_source_trade",
        ),
    )
    op.create_index(
        "ix_dhan_trade_mirror_user_time",
        "dhan_trade_mirror",
        ["user_id", "trade_at"],
    )
    op.create_table(
        "dhan_ledger_mirror",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("source", sa.String(length=32), server_default="dhan", nullable=False),
        sa.Column("entry_id", sa.String(length=64), nullable=False),
        sa.Column("entry_date", sa.DateTime(timezone=True), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("debit", sa.Numeric(precision=24, scale=2), nullable=True),
        sa.Column("credit", sa.Numeric(precision=24, scale=2), nullable=True),
        sa.Column("running_balance", sa.Numeric(precision=24, scale=2), nullable=True),
        sa.Column("currency", sa.String(length=3), server_default="INR", nullable=False),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id", "source", "entry_id",
            name="uq_dhan_ledger_mirror_user_source_entry",
        ),
    )
    op.create_index(
        "ix_dhan_ledger_mirror_user_date",
        "dhan_ledger_mirror",
        ["user_id", "entry_date"],
    )


def downgrade() -> None:
    op.drop_index("ix_dhan_ledger_mirror_user_date", table_name="dhan_ledger_mirror")
    op.drop_table("dhan_ledger_mirror")
    op.drop_index("ix_dhan_trade_mirror_user_time", table_name="dhan_trade_mirror")
    op.drop_table("dhan_trade_mirror")
