"""Filing dates for NSE-sourced statements and shareholding, and a small listings table.

Revision ID: 0067_nse_data_and_listings
Revises: 0066_multibagger_assessments

Additive: nullable columns and one new table; nothing existing is changed or dropped.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0067_nse_data_and_listings"
down_revision: Union[str, None] = "0066_multibagger_assessments"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("instrument_fundamentals", sa.Column("filed_on", sa.Date(), nullable=True))
    op.add_column("shareholding_snapshots", sa.Column("quarter_end", sa.Date(), nullable=True))
    op.add_column("shareholding_snapshots", sa.Column("filed_on", sa.Date(), nullable=True))
    op.create_table(
        "instrument_listings",
        sa.Column("id", sa.Uuid(), nullable=False), sa.Column("instrument_id", sa.Uuid(), nullable=False), sa.Column("issue_price", sa.Numeric(24, 8), nullable=True),
        sa.Column("allotment_date", sa.Date(), nullable=True), sa.Column("listing_date", sa.Date(), nullable=True), sa.Column("pre_ipo_lockin_end", sa.Date(), nullable=True),
        sa.Column("source", sa.String(64), nullable=False), sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"], ondelete="CASCADE"), sa.PrimaryKeyConstraint("id"), sa.UniqueConstraint("instrument_id", name="uq_instrument_listings_instrument"),
    )


def downgrade() -> None:
    op.drop_table("instrument_listings")
    op.drop_column("shareholding_snapshots", "filed_on")
    op.drop_column("shareholding_snapshots", "quarter_end")
    op.drop_column("instrument_fundamentals", "filed_on")
