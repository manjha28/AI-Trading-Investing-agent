"""Alert rules: last evaluation status and reason.

Revision ID: 0057_alert_rule_status
Revises: 0056_var_run_statuses

Additive: three nullable columns; nothing is dropped or rewritten.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0057_alert_rule_status"
down_revision: Union[str, None] = "0056_var_run_statuses"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("alert_rules", sa.Column("last_evaluated_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("alert_rules", sa.Column("last_status", sa.String(length=16), nullable=True))
    op.add_column("alert_rules", sa.Column("last_reason", sa.String(length=200), nullable=True))


def downgrade() -> None:
    op.drop_column("alert_rules", "last_reason")
    op.drop_column("alert_rules", "last_status")
    op.drop_column("alert_rules", "last_evaluated_at")
