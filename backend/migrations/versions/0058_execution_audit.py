"""Execution audit trail (append-only): intent, decision and fill events.

Revision ID: 0058_execution_audit
Revises: 0057_alert_rule_status

Additive: one new table. No order is ever transmitted; rows record research/monitor-mode activity.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0058_execution_audit"
down_revision: Union[str, None] = "0057_alert_rule_status"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "execution_audit_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("event_type", sa.String(16), nullable=False),
        sa.Column("mode", sa.String(12), nullable=False),
        sa.Column("correlation_id", sa.Uuid(), nullable=False),
        sa.Column("client_order_id", sa.String(40), nullable=False),
        sa.Column("venue", sa.String(32), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("hypothetical", sa.Boolean(), nullable=False),
        sa.Column("payload", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False),
    )
    op.create_index("ix_execution_audit_user_created", "execution_audit_events", ["user_id", "created_at"])
    op.create_index("ix_execution_audit_correlation", "execution_audit_events", ["correlation_id"])


def downgrade() -> None:
    op.drop_index("ix_execution_audit_correlation", table_name="execution_audit_events")
    op.drop_index("ix_execution_audit_user_created", table_name="execution_audit_events")
    op.drop_table("execution_audit_events")
