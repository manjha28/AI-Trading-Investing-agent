"""VAR runs: explicit non-success outcomes (no_usable_data, order_flow_unavailable).

Revision ID: 0056_var_run_statuses
Revises: 0055_var_strategy

Additive: only widens an existing CHECK constraint (no column or data is dropped). Downgrade restores the narrower set only if no row uses the new values.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0056_var_run_statuses"
down_revision: Union[str, None] = "0055_var_strategy"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OLD = "status IN ('queued','running','completed','failed','unavailable')"
NEW = "status IN ('queued','running','completed','failed','unavailable','no_usable_data','order_flow_unavailable')"


def upgrade() -> None:
    op.drop_constraint("ck_var_backtest_status", "var_backtest_runs", type_="check")
    op.create_check_constraint("ck_var_backtest_status", "var_backtest_runs", NEW)


def downgrade() -> None:
    op.execute("UPDATE var_backtest_runs SET status = 'unavailable' WHERE status IN ('no_usable_data','order_flow_unavailable')")
    op.drop_constraint("ck_var_backtest_status", "var_backtest_runs", type_="check")
    op.create_check_constraint("ck_var_backtest_status", "var_backtest_runs", OLD)
