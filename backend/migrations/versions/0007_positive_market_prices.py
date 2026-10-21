"""Require positive OHLC values for persisted market bars.

Revision ID: 0007_positive_market_prices
Revises: 0006_instruments_market_data
"""

from typing import Sequence, Union

from alembic import context, op
import sqlalchemy as sa

revision: str = "0007_positive_market_prices"
down_revision: Union[str, None] = "0006_instruments_market_data"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    has_constraint = False
    if not context.is_offline_mode():
        constraints = sa.inspect(op.get_bind()).get_check_constraints("market_bars")
        has_constraint = any(
            constraint["name"] == "ck_market_bars_positive_prices"
            for constraint in constraints
        )
    if not has_constraint:
        op.create_check_constraint(
            "ck_market_bars_positive_prices",
            "market_bars",
            "open > 0 AND high > 0 AND low > 0 AND close > 0",
        )


def downgrade() -> None:
    op.drop_constraint(
        "ck_market_bars_positive_prices",
        "market_bars",
        type_="check",
    )
