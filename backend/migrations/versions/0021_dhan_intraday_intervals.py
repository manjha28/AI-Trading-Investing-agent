"""Support Dhan intraday candle intervals.

Revision ID: 0021_dhan_intraday_intervals
Revises: 0020_market_data_backfill
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0021_dhan_intraday_intervals"
down_revision: Union[str, None] = "0020_market_data_backfill"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_INTERVALS = "interval IN ('1d', '1h', '25m', '15m', '5m', '1m')"


def upgrade() -> None:
    with op.batch_alter_table("market_bars") as batch:
        batch.drop_constraint("ck_market_bars_interval", type_="check")
        batch.create_check_constraint("ck_market_bars_interval", _INTERVALS)
    with op.batch_alter_table("market_data_sync_attempts") as batch:
        batch.drop_constraint("ck_market_sync_attempt_interval", type_="check")
        batch.create_check_constraint("ck_market_sync_attempt_interval", _INTERVALS)


def downgrade() -> None:
    with op.batch_alter_table("market_data_sync_attempts") as batch:
        batch.drop_constraint("ck_market_sync_attempt_interval", type_="check")
        batch.create_check_constraint(
            "ck_market_sync_attempt_interval",
            "interval IN ('1d', '1h', '15m', '1m')",
        )
    with op.batch_alter_table("market_bars") as batch:
        batch.drop_constraint("ck_market_bars_interval", type_="check")
        batch.create_check_constraint(
            "ck_market_bars_interval",
            "interval IN ('1d', '1h', '15m', '1m')",
        )
