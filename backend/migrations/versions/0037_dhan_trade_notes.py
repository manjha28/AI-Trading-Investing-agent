"""Persist owner commentary on imported Dhan fills.

Revision ID: 0037_dhan_trade_notes
Revises: 0036_portfolio_valuation_history
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0037_dhan_trade_notes"
down_revision: Union[str, None] = "0036_portfolio_valuation_history"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("dhan_trade_mirror", sa.Column("notes", sa.String(length=4000), nullable=True))
    op.add_column(
        "dhan_trade_mirror",
        sa.Column("tags", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
    )
    op.alter_column("dhan_trade_mirror", "tags", server_default=None)


def downgrade() -> None:
    op.drop_column("dhan_trade_mirror", "tags")
    op.drop_column("dhan_trade_mirror", "notes")
