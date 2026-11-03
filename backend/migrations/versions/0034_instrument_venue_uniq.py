"""Enforce one instrument row per venue and symbol.

Revision ID: 0034_instrument_venue_uniq
Revises: 0033_strategy_signals
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0034_instrument_venue_uniq"
down_revision: Union[str, None] = "0033_strategy_signals"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "uq_instruments_venue_symbol",
        "instruments",
        ["venue", "symbol"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_instruments_venue_symbol", table_name="instruments")
