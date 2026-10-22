"""Add instrument fundamentals and snapshots.

Revision ID: 0018_company_fundamentals
Revises: 0017_alerts
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0018_company_fundamentals"
down_revision: Union[str, None] = "0017_alerts"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "instrument_fundamentals",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("instrument_id", sa.Uuid(), nullable=False),
        sa.Column("period_type", sa.String(length=12), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("currency", sa.String(length=4), nullable=False),
        sa.Column("revenue", sa.Numeric(24, 8), nullable=True),
        sa.Column("net_income", sa.Numeric(24, 8), nullable=True),
        sa.Column("eps", sa.Numeric(24, 8), nullable=True),
        sa.Column("operating_margin", sa.Numeric(18, 8), nullable=True),
        sa.Column("total_assets", sa.Numeric(24, 8), nullable=True),
        sa.Column("total_liabilities", sa.Numeric(24, 8), nullable=True),
        sa.Column("total_equity", sa.Numeric(24, 8), nullable=True),
        sa.Column("operating_cash_flow", sa.Numeric(24, 8), nullable=True),
        sa.Column("free_cash_flow", sa.Numeric(24, 8), nullable=True),
        sa.Column("total_debt", sa.Numeric(24, 8), nullable=True),
        sa.Column("shares_outstanding", sa.Numeric(24, 8), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "period_type IN ('annual', 'quarterly')",
            name="ck_instrument_fundamentals_period_type",
        ),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "instrument_id", "period_type", "period_end",
            name="uq_instrument_fundamentals_period",
        ),
    )
    op.create_index(
        "ix_instrument_fundamentals_instrument_period",
        "instrument_fundamentals",
        ["instrument_id", "period_type", "period_end"],
    )
    op.create_table(
        "instrument_snapshots",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("instrument_id", sa.Uuid(), nullable=False),
        sa.Column("market_cap", sa.Numeric(28, 8), nullable=True),
        sa.Column("pe", sa.Numeric(24, 8), nullable=True),
        sa.Column("pb", sa.Numeric(24, 8), nullable=True),
        sa.Column("dividend_yield", sa.Numeric(18, 8), nullable=True),
        sa.Column("sector", sa.String(length=200), nullable=True),
        sa.Column("industry", sa.String(length=200), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("instrument_id", name="uq_instrument_snapshots_instrument"),
    )


def downgrade() -> None:
    op.drop_table("instrument_snapshots")
    op.drop_index(
        "ix_instrument_fundamentals_instrument_period",
        table_name="instrument_fundamentals",
    )
    op.drop_table("instrument_fundamentals")
