"""Persist Dhan position classification and broker P&L.

Revision ID: 0032_broker_position_metrics
Revises: 0031_named_watchlists
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0032_broker_position_metrics"
down_revision: Union[str, None] = "0031_named_watchlists"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "broker_mirror",
        sa.Column("position_type", sa.String(length=16), nullable=True),
    )
    op.add_column(
        "broker_mirror",
        sa.Column("realized_profit", sa.Numeric(precision=24, scale=8), nullable=True),
    )
    op.add_column(
        "broker_mirror",
        sa.Column("unrealized_profit", sa.Numeric(precision=24, scale=8), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("broker_mirror", "unrealized_profit")
    op.drop_column("broker_mirror", "realized_profit")
    op.drop_column("broker_mirror", "position_type")
