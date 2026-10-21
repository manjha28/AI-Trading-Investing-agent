"""Add a portfolio transaction ledger.

Revision ID: 0003_portfolio_transactions
Revises: 0002_users_sessions_portfolios
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0003_portfolio_transactions"
down_revision: Union[str, None] = "0002_users_sessions_portfolios"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "portfolio_transactions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("portfolio_id", sa.Uuid(), nullable=False),
        sa.Column("transaction_type", sa.String(length=16), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=True),
        sa.Column("quantity", sa.Numeric(24, 8), nullable=True),
        sa.Column("unit_price", sa.Numeric(24, 8), nullable=True),
        sa.Column("amount", sa.Numeric(24, 2), nullable=False),
        sa.Column("fee", sa.Numeric(24, 2), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "transaction_type IN ('deposit', 'withdrawal', 'buy', 'sell', 'dividend')",
            name="ck_portfolio_transactions_type",
        ),
        sa.CheckConstraint("amount >= 0 AND fee >= 0", name="ck_portfolio_transactions_amounts"),
    )
    op.create_index(
        "ix_portfolio_transactions_portfolio_time",
        "portfolio_transactions",
        ["portfolio_id", "occurred_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_portfolio_transactions_portfolio_time", table_name="portfolio_transactions")
    op.drop_table("portfolio_transactions")