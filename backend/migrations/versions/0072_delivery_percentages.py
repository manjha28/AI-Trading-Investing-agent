"""Delivery percentage per symbol and session (NSE full bhavcopy).

Revision ID: 0072_delivery_percentages
Revises: 0071_forward_record_reports

Additive: one new table; nothing existing is changed.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0072_delivery_percentages"
down_revision: Union[str, None] = "0071_forward_record_reports"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "delivery_percentages",
        sa.Column("symbol", sa.String(32), nullable=False), sa.Column("session_date", sa.Date(), nullable=False), sa.Column("series", sa.String(4), nullable=False),
        sa.Column("deliv_per", sa.Numeric(7, 2), nullable=False), sa.Column("source", sa.String(24), nullable=False),
        sa.PrimaryKeyConstraint("symbol", "session_date", "series"),
    )


def downgrade() -> None:
    op.drop_table("delivery_percentages")
