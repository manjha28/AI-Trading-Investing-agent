"""Add sourced research notes with provenance.

Revision ID: 0015_research_sources
Revises: 0014_corporate_actions
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0015_research_sources"
down_revision: Union[str, None] = "0014_corporate_actions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSON_DOCUMENT = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "research_sources",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("instrument_id", sa.Uuid(), nullable=True),
        sa.Column("url", sa.String(length=2048), nullable=False),
        sa.Column("publisher", sa.String(length=200), nullable=True),
        sa.Column("title", sa.String(length=500), nullable=True),
        sa.Column("author", sa.String(length=200), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("excerpt", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "content_hash", name="uq_research_sources_user_hash"),
    )
    op.create_index(
        "ix_research_sources_user_instrument", "research_sources", ["user_id", "instrument_id"]
    )
    op.create_table(
        "research_notes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("instrument_id", sa.Uuid(), nullable=False),
        sa.Column("as_of", sa.Date(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("key_facts", JSON_DOCUMENT, nullable=False),
        sa.Column("source_ids", JSON_DOCUMENT, nullable=False),
        sa.Column("confidence", sa.String(length=8), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "confidence IN ('low', 'medium', 'high')", name="ck_research_notes_confidence"
        ),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_research_notes_user_instrument", "research_notes", ["user_id", "instrument_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_research_notes_user_instrument", table_name="research_notes")
    op.drop_table("research_notes")
    op.drop_index("ix_research_sources_user_instrument", table_name="research_sources")
    op.drop_table("research_sources")
