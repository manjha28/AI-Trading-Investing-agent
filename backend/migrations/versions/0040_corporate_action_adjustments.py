"""Add source verification and adjustment coverage for corporate actions.

Revision ID: 0040_corp_action_adjust
Revises: 0039_stream_tick_gaps
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0040_corp_action_adjust"
down_revision: Union[str, None] = "0039_stream_tick_gaps"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint("ck_corporate_actions_type", "corporate_actions", type_="check")
    op.drop_constraint("ck_corporate_actions_values", "corporate_actions", type_="check")
    op.create_check_constraint(
        "ck_corporate_actions_type",
        "corporate_actions",
        "action_type IN ('split', 'bonus', 'dividend', 'rights')",
    )
    op.create_check_constraint(
        "ck_corporate_actions_values",
        "corporate_actions",
        "(action_type IN ('split', 'bonus') AND ratio IS NOT NULL AND ratio > 0 AND amount IS NULL) "
        "OR (action_type = 'dividend' AND ratio IS NULL AND amount IS NOT NULL AND amount > 0) "
        "OR (action_type = 'rights' AND ratio IS NOT NULL AND ratio > 0 AND amount IS NOT NULL AND amount > 0)",
    )
    op.add_column("corporate_actions", sa.Column("source_reference", sa.String(512), nullable=True))
    op.add_column("corporate_actions", sa.Column("announcement_date", sa.Date(), nullable=True))
    op.add_column("corporate_actions", sa.Column("record_date", sa.Date(), nullable=True))
    op.add_column("corporate_actions", sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("corporate_actions", sa.Column("verified_by", sa.Uuid(), nullable=True))
    op.add_column(
        "corporate_actions",
        sa.Column("is_discrepant", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("corporate_actions", sa.Column("discrepancy_note", sa.String(500), nullable=True))
    op.create_foreign_key(
        "fk_corporate_actions_verified_by_users",
        "corporate_actions",
        "users",
        ["verified_by"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_table(
        "corporate_action_coverage",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("instrument_id", sa.Uuid(), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_reference", sa.String(512), nullable=True),
        sa.Column("detail", sa.String(500), nullable=True),
        sa.CheckConstraint("status IN ('verified', 'unverified')", name="ck_corporate_action_coverage_status"),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id", "instrument_id", "source",
            name="uq_corporate_action_coverage_owner_instrument_source",
        ),
    )


def downgrade() -> None:
    op.drop_table("corporate_action_coverage")
    op.drop_constraint("fk_corporate_actions_verified_by_users", "corporate_actions", type_="foreignkey")
    op.drop_column("corporate_actions", "discrepancy_note")
    op.drop_column("corporate_actions", "is_discrepant")
    op.drop_column("corporate_actions", "verified_by")
    op.drop_column("corporate_actions", "verified_at")
    op.drop_column("corporate_actions", "record_date")
    op.drop_column("corporate_actions", "announcement_date")
    op.drop_column("corporate_actions", "source_reference")
    op.drop_constraint("ck_corporate_actions_type", "corporate_actions", type_="check")
    op.drop_constraint("ck_corporate_actions_values", "corporate_actions", type_="check")
    op.create_check_constraint(
        "ck_corporate_actions_type",
        "corporate_actions",
        "action_type IN ('split', 'bonus', 'dividend')",
    )
    op.create_check_constraint(
        "ck_corporate_actions_values",
        "corporate_actions",
        "(action_type IN ('split', 'bonus') AND ratio IS NOT NULL AND ratio > 0 AND amount IS NULL) "
        "OR (action_type = 'dividend' AND ratio IS NULL AND amount IS NOT NULL AND amount > 0)",
    )
