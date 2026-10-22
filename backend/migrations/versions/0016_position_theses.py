"""Add position theses with revision history.

Revision ID: 0016_position_theses
Revises: 0015_research_sources
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0016_position_theses"
down_revision: Union[str, None] = "0015_research_sources"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSON_DOCUMENT = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "position_theses",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("portfolio_id", sa.Uuid(), nullable=False),
        sa.Column("instrument_id", sa.Uuid(), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=12), nullable=False),
        sa.Column("entry_reason", sa.Text(), nullable=False),
        sa.Column("horizon_days", sa.Integer(), nullable=False),
        sa.Column("conviction", sa.Integer(), nullable=False),
        sa.Column("target_levels", JSON_DOCUMENT, nullable=False),
        sa.Column("stop_level", sa.Numeric(24, 8), nullable=True),
        sa.Column("invalidation_conditions", JSON_DOCUMENT, nullable=False),
        sa.Column("invalidation_notes", sa.Text(), nullable=True),
        sa.Column("max_loss_pct", sa.Numeric(7, 4), nullable=True),
        sa.Column("review_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('active', 'invalidated', 'closed')", name="ck_position_theses_status"
        ),
        sa.CheckConstraint("conviction BETWEEN 1 AND 5", name="ck_position_theses_conviction"),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_position_theses_active",
        "position_theses",
        ["user_id", "portfolio_id", "instrument_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
        sqlite_where=sa.text("status = 'active'"),
    )
    op.create_index(
        "ix_position_theses_user_portfolio", "position_theses", ["user_id", "portfolio_id"]
    )
    op.create_table(
        "position_thesis_revisions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("thesis_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("changed_fields", JSON_DOCUMENT, nullable=False),
        sa.Column("snapshot", JSON_DOCUMENT, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["thesis_id"], ["position_theses.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "thesis_id", "revision_number", name="uq_position_thesis_revisions_number"
        ),
    )


def downgrade() -> None:
    op.drop_table("position_thesis_revisions")
    op.drop_index("ix_position_theses_user_portfolio", table_name="position_theses")
    op.drop_index("uq_position_theses_active", table_name="position_theses")
    op.drop_table("position_theses")
