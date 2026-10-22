"""Add user-scoped corporate actions and reversible portfolio applications.

Revision ID: 0014_corporate_actions
Revises: 0013_app_two_factor
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0014_corporate_actions"
down_revision: Union[str, None] = "0013_app_two_factor"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "corporate_actions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("instrument_id", sa.Uuid(), nullable=False),
        sa.Column("action_type", sa.String(length=12), nullable=False),
        sa.Column("ex_date", sa.Date(), nullable=False),
        sa.Column("ratio", sa.Numeric(18, 8), nullable=True),
        sa.Column("amount", sa.Numeric(24, 8), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "action_type IN ('split', 'bonus', 'dividend')",
            name="ck_corporate_actions_type",
        ),
        sa.CheckConstraint(
            "(action_type IN ('split', 'bonus') AND ratio IS NOT NULL AND ratio > 0 AND amount IS NULL) "
            "OR (action_type = 'dividend' AND ratio IS NULL AND amount IS NOT NULL AND amount > 0)",
            name="ck_corporate_actions_values",
        ),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_corporate_actions_user_instrument_date",
        "corporate_actions",
        ["user_id", "instrument_id", "ex_date"],
    )
    op.create_table(
        "corporate_action_applications",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("portfolio_id", sa.Uuid(), nullable=False),
        sa.Column("corporate_action_id", sa.Uuid(), nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reversed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("affected_quantity", sa.Numeric(24, 8), nullable=False),
        sa.Column("quantity_adjustment", sa.Numeric(24, 8), nullable=False),
        sa.Column("cash_adjustment", sa.Numeric(24, 2), nullable=False),
        sa.ForeignKeyConstraint(["corporate_action_id"], ["corporate_actions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_corporate_action_applications_active",
        "corporate_action_applications",
        ["portfolio_id", "corporate_action_id"],
        unique=True,
        postgresql_where=sa.text("reversed_at IS NULL"),
        sqlite_where=sa.text("reversed_at IS NULL"),
    )
    op.create_index(
        "ix_corporate_action_applications_user_portfolio",
        "corporate_action_applications",
        ["user_id", "portfolio_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_corporate_action_applications_user_portfolio",
        table_name="corporate_action_applications",
    )
    op.drop_index(
        "uq_corporate_action_applications_active",
        table_name="corporate_action_applications",
    )
    op.drop_table("corporate_action_applications")
    op.drop_index(
        "ix_corporate_actions_user_instrument_date",
        table_name="corporate_actions",
    )
    op.drop_table("corporate_actions")
