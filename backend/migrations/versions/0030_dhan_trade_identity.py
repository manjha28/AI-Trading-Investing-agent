"""Retain Dhan provider IDs alongside stable execution keys.

Revision ID: 0030_dhan_trade_identity
Revises: 0029_dhan_ledger_line_key
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0030_dhan_trade_identity"
down_revision: Union[str, None] = "0029_dhan_ledger_line_key"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "dhan_trade_mirror",
        sa.Column("provider_trade_id", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "dhan_trade_mirror",
        sa.Column("exchange_order_id", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("dhan_trade_mirror", "exchange_order_id")
    op.drop_column("dhan_trade_mirror", "provider_trade_id")
