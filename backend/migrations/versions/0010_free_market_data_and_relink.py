"""Support free-provider instruments, 1m bars, and relink notices.

Revision ID: 0010_free_market_data_and_relink
Revises: 0009_market_data_portfolio_links
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0010_free_market_data_and_relink"
down_revision: Union[str, None] = "0009_market_data_portfolio_links"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("instruments") as batch:
        batch.drop_constraint("ck_instruments_currency_length", type_="check")
        batch.alter_column("currency", existing_type=sa.String(length=3), type_=sa.String(length=4))
        batch.create_check_constraint(
            "ck_instruments_currency_length", "length(currency) IN (3, 4)"
        )
        batch.add_column(
            sa.Column("relink_required", sa.Boolean(), nullable=False, server_default=sa.false())
        )
    with op.batch_alter_table("market_bars") as batch:
        batch.drop_constraint("ck_market_bars_interval", type_="check")
        batch.create_check_constraint(
            "ck_market_bars_interval", "interval IN ('1d', '1h', '15m', '1m')"
        )
    with op.batch_alter_table("market_data_syncs") as batch:
        batch.drop_constraint("ck_market_sync_interval", type_="check")
        batch.create_check_constraint(
            "ck_market_sync_interval", "interval IN ('1d', '1h', '15m', '1m')"
        )


def downgrade() -> None:
    with op.batch_alter_table("market_data_syncs") as batch:
        batch.drop_constraint("ck_market_sync_interval", type_="check")
        batch.create_check_constraint(
            "ck_market_sync_interval", "interval IN ('1d', '1h', '15m')"
        )
    with op.batch_alter_table("market_bars") as batch:
        batch.drop_constraint("ck_market_bars_interval", type_="check")
        batch.create_check_constraint(
            "ck_market_bars_interval", "interval IN ('1d', '1h', '15m')"
        )
    with op.batch_alter_table("instruments") as batch:
        batch.drop_column("relink_required")
        batch.drop_constraint("ck_instruments_currency_length", type_="check")
        batch.alter_column("currency", existing_type=sa.String(length=4), type_=sa.String(length=3))
        batch.create_check_constraint("ck_instruments_currency_length", "length(currency) = 3")
