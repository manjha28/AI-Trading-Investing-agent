"""Add stored instrument research runs, briefs, and custom source links.

Revision ID: 0019_instrument_research_pipeline
Revises: 0018_company_fundamentals
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0019_research_pipeline"
down_revision: Union[str, None] = "0018_company_fundamentals"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSON_DOCUMENT = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "instrument_research_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("instrument_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=12), nullable=False),
        sa.Column("sources_attempted", sa.Integer(), nullable=False),
        sa.Column("sources_succeeded", sa.Integer(), nullable=False),
        sa.Column("failures", JSON_DOCUMENT, nullable=False),
        sa.CheckConstraint(
            "status IN ('running', 'complete', 'partial', 'failed')",
            name="ck_instrument_research_runs_status",
        ),
        sa.CheckConstraint(
            "sources_attempted >= 0 AND sources_succeeded >= 0 "
            "AND sources_succeeded <= sources_attempted",
            name="ck_instrument_research_runs_source_counts",
        ),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_instrument_research_runs_user_instrument_started",
        "instrument_research_runs",
        ["user_id", "instrument_id", "started_at"],
    )
    op.create_table(
        "instrument_research_briefs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("instrument_id", sa.Uuid(), nullable=False),
        sa.Column("sections", JSON_DOCUMENT, nullable=False),
        sa.Column("renderer", sa.String(length=12), nullable=False),
        sa.Column("data_quality", JSON_DOCUMENT, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "renderer IN ('template', 'ai')",
            name="ck_instrument_research_briefs_renderer",
        ),
        sa.ForeignKeyConstraint(
            ["instrument_id"], ["instruments.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["instrument_research_runs.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", name="uq_instrument_research_briefs_run"),
    )
    op.create_index(
        "ix_instrument_research_briefs_instrument_created",
        "instrument_research_briefs",
        ["instrument_id", "created_at"],
    )
    op.create_table(
        "instrument_source_links",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("instrument_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("url", sa.String(length=2048), nullable=False),
        sa.Column("label", sa.String(length=200), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["instrument_id"], ["instruments.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id",
            "instrument_id",
            "url",
            name="uq_instrument_source_links_user_instrument_url",
        ),
    )
    op.create_index(
        "ix_instrument_source_links_user_instrument",
        "instrument_source_links",
        ["user_id", "instrument_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_instrument_source_links_user_instrument",
        table_name="instrument_source_links",
    )
    op.drop_table("instrument_source_links")
    op.drop_index(
        "ix_instrument_research_briefs_instrument_created",
        table_name="instrument_research_briefs",
    )
    op.drop_table("instrument_research_briefs")
    op.drop_index(
        "ix_instrument_research_runs_user_instrument_started",
        table_name="instrument_research_runs",
    )
    op.drop_table("instrument_research_runs")
