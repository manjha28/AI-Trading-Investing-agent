"""Link portfolio transactions and watchlists to instruments.

Revision ID: 0009_market_data_portfolio_links
Revises: 0008_market_quote_cache
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0009_market_data_portfolio_links"
down_revision: Union[str, None] = "0008_market_quote_cache"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "portfolio_transactions",
        sa.Column("instrument_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_portfolio_transactions_instrument_id_instruments",
        "portfolio_transactions",
        "instruments",
        ["instrument_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_portfolio_transactions_instrument_id",
        "portfolio_transactions",
        ["instrument_id"],
    )
    op.create_table(
        "watchlist_instruments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("instrument_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "instrument_id", name="uq_watchlist_user_instrument"),
    )
    op.create_index(
        "ix_watchlist_instruments_instrument",
        "watchlist_instruments",
        ["instrument_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_watchlist_instruments_instrument", table_name="watchlist_instruments")
    op.drop_table("watchlist_instruments")
    op.drop_index(
        "ix_portfolio_transactions_instrument_id",
        table_name="portfolio_transactions",
    )
    op.drop_constraint(
        "fk_portfolio_transactions_instrument_id_instruments",
        "portfolio_transactions",
        type_="foreignkey",
    )
    op.drop_column("portfolio_transactions", "instrument_id")
