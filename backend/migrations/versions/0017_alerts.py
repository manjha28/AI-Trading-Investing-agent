"""Add user-scoped alert rules and explainable events.

Revision ID: 0017_alerts
Revises: 0016_position_theses
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0017_alerts"
down_revision: Union[str, None] = "0016_position_theses"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSON_DOCUMENT = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "alert_rules",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("scope", sa.String(length=12), nullable=False),
        sa.Column("instrument_id", sa.Uuid(), nullable=True),
        sa.Column("portfolio_id", sa.Uuid(), nullable=True),
        sa.Column("rule_type", sa.String(length=40), nullable=False),
        sa.Column("params", JSON_DOCUMENT, nullable=False),
        sa.Column("severity", sa.String(length=12), nullable=False),
        sa.Column("cooldown_seconds", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "scope IN ('instrument', 'position', 'portfolio')",
            name="ck_alert_rules_scope",
        ),
        sa.CheckConstraint(
            "severity IN ('info', 'attention', 'urgent')",
            name="ck_alert_rules_severity",
        ),
        sa.CheckConstraint("cooldown_seconds >= 0", name="ck_alert_rules_cooldown"),
        sa.CheckConstraint(
            "(scope = 'instrument' AND instrument_id IS NOT NULL) OR "
            "(scope = 'position' AND portfolio_id IS NOT NULL AND instrument_id IS NOT NULL) OR "
            "(scope = 'portfolio' AND portfolio_id IS NOT NULL)",
            name="ck_alert_rules_scope_target",
        ),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_alert_rules_user_enabled", "alert_rules", ["user_id", "enabled"])
    op.create_table(
        "alert_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("rule_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("instrument_id", sa.Uuid(), nullable=True),
        sa.Column("fired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("dedupe_key", sa.String(length=160), nullable=False),
        sa.Column("evidence", JSON_DOCUMENT, nullable=False),
        sa.Column("status", sa.String(length=12), nullable=False),
        sa.CheckConstraint(
            "status IN ('new', 'read', 'dismissed', 'actioned')",
            name="ck_alert_events_status",
        ),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["rule_id"], ["alert_rules.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("rule_id", "dedupe_key", name="uq_alert_events_rule_dedupe"),
    )
    op.create_index("ix_alert_events_user_fired", "alert_events", ["user_id", "fired_at"])


def downgrade() -> None:
    op.drop_index("ix_alert_events_user_fired", table_name="alert_events")
    op.drop_table("alert_events")
    op.drop_index("ix_alert_rules_user_enabled", table_name="alert_rules")
    op.drop_table("alert_rules")
