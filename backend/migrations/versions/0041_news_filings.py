"""Store news and corporate filings with source publication timestamps."""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0041_news_filings"
down_revision: Union[str, None] = "0040_corp_action_adjust"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

def upgrade() -> None:
    op.create_table(
        "news_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("external_id", sa.String(256), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("source_url", sa.String(2048)),
        sa.Column("instrument_id", sa.Uuid()),
        sa.Column("symbol_raw", sa.String(96)),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("publisher", sa.String(200)),
        sa.Column("category", sa.String(160)),
        sa.Column("headline", sa.String(1000), nullable=False),
        sa.Column("body", sa.Text()),
        sa.Column("raw_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source", "external_id", name="uq_news_items_source_external_id"),
    )
    op.create_index("ix_news_items_published", "news_items", ["published_at"])
    op.create_index("ix_news_items_instrument_published", "news_items", ["instrument_id", "published_at"])
    op.create_table(
        "news_ingestion_failures",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("symbol_raw", sa.String(96)),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("detail", sa.String(1000), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

def downgrade() -> None:
    op.drop_table("news_ingestion_failures")
    op.drop_index("ix_news_items_instrument_published", table_name="news_items")
    op.drop_index("ix_news_items_published", table_name="news_items")
    op.drop_table("news_items")
