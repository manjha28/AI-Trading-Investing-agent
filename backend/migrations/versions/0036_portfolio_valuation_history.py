"""Store forward-only portfolio valuation history.

Revision ID: 0036_portfolio_valuation_history
Revises: 0035_instrument_identity_uniq
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0036_portfolio_valuation_history"
down_revision: Union[str, None] = "0035_instrument_identity_uniq"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "portfolio_valuation_snapshots",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("portfolio_id", sa.Uuid(), nullable=True),
        sa.Column("scope", sa.String(length=12), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("market_value", sa.Numeric(precision=24, scale=2), nullable=True),
        sa.Column("unrealized_pnl", sa.Numeric(precision=24, scale=2), nullable=True),
        sa.Column("day_change", sa.Numeric(precision=24, scale=2), nullable=True),
        sa.Column("invested", sa.Numeric(precision=24, scale=2), nullable=True),
        sa.Column("valuation_basis", sa.String(length=24), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("provider_timestamp", sa.DateTime(timezone=True), nullable=True),
        sa.Column("complete", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "price_observations",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "scope IN ('owner', 'portfolio')",
            name="ck_portfolio_valuation_snapshots_scope",
        ),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "owner_id",
            "portfolio_id",
            "timestamp",
            name="uq_portfolio_valuation_snapshots_owner_portfolio_time",
            postgresql_nulls_not_distinct=True,
        ),
    )
    op.create_index(
        "ix_portfolio_valuation_snapshots_owner_scope_time",
        "portfolio_valuation_snapshots",
        ["owner_id", "scope", "timestamp"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_portfolio_valuation_snapshots_owner_scope_time",
        table_name="portfolio_valuation_snapshots",
    )
    op.drop_table("portfolio_valuation_snapshots")
