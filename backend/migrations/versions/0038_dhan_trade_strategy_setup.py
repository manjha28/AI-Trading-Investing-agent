"""Persist strategy and setup annotations on imported Dhan fills.

Revision ID: 0038_dhan_trade_strategy_setup
Revises: 0037_dhan_trade_notes
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0038_dhan_trade_strategy_setup"
down_revision: Union[str, None] = "0037_dhan_trade_notes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, None] = None


def upgrade() -> None:
    op.add_column("dhan_trade_mirror", sa.Column("strategy", sa.String(length=100), nullable=True))
    op.add_column("dhan_trade_mirror", sa.Column("setup", sa.String(length=100), nullable=True))


def downgrade() -> None:
    op.drop_column("dhan_trade_mirror", "setup")
    op.drop_column("dhan_trade_mirror", "strategy")
