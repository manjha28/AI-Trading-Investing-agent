"""Restrict market history to provider-supported intervals.

Revision ID: 0022_market_data_intervals
Revises: 0021_dhan_intraday_intervals
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0022_market_data_intervals"
down_revision: Union[str, None] = "0021_dhan_intraday_intervals"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_INTERVALS = "interval IN ('1d', '1h', '15m', '5m', '1m')"


def upgrade() -> None:
    for table, constraint in (
        ("market_bars", "ck_market_bars_interval"),
        ("market_data_syncs", "ck_market_sync_interval"),
        ("market_data_sync_attempts", "ck_market_sync_attempt_interval"),
    ):
        with op.batch_alter_table(table) as batch:
            batch.drop_constraint(constraint, type_="check")
            batch.create_check_constraint(constraint, _INTERVALS)


def downgrade() -> None:
    prior_constraints = {
        "market_bars": (
            "ck_market_bars_interval",
            "interval IN ('1d', '1h', '25m', '15m', '5m', '1m')",
        ),
        "market_data_syncs": (
            "ck_market_sync_interval",
            "interval IN ('1d', '1h', '15m', '1m')",
        ),
        "market_data_sync_attempts": (
            "ck_market_sync_attempt_interval",
            "interval IN ('1d', '1h', '25m', '15m', '5m', '1m')",
        ),
    }
    for table, (constraint, expression) in prior_constraints.items():
        with op.batch_alter_table(table) as batch:
            batch.drop_constraint(constraint, type_="check")
            batch.create_check_constraint(constraint, expression)
