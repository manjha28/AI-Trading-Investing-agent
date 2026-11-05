"""Persist versioned, evidence-linked sentiment analysis per news item."""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0043_news_sentiment"
down_revision: Union[str, None] = "0042_news_source_health"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "news_sentiment_analyses",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("news_item_id", sa.Uuid(), nullable=False),
        sa.Column("classifier_version", sa.String(32), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("analyzed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sentiment", sa.String(16), nullable=False),
        sa.Column("sentiment_score", sa.Numeric(6, 4), nullable=False),
        sa.Column("strength", sa.Numeric(5, 4), nullable=False),
        sa.Column("impact", sa.String(8), nullable=False),
        sa.Column("confidence_score", sa.Integer(), nullable=False),
        sa.Column("event_category", sa.String(64), nullable=False),
        sa.Column("relevance_scope", sa.String(16), nullable=False),
        sa.Column("interpretation", sa.String(16), nullable=False),
        sa.Column("explanation", sa.Text(), nullable=False),
        sa.Column("evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("event_key", sa.String(64), nullable=False),
        sa.CheckConstraint("sentiment IN ('positive', 'negative', 'neutral', 'mixed')", name="ck_news_sentiment_label"),
        sa.CheckConstraint("sentiment_score >= -1 AND sentiment_score <= 1", name="ck_news_sentiment_score"),
        sa.CheckConstraint("strength >= 0 AND strength <= 1", name="ck_news_sentiment_strength"),
        sa.CheckConstraint("confidence_score >= 0 AND confidence_score <= 100", name="ck_news_sentiment_confidence"),
        sa.CheckConstraint("impact IN ('low', 'medium', 'high')", name="ck_news_sentiment_impact"),
        sa.ForeignKeyConstraint(["news_item_id"], ["news_items.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("news_item_id", "classifier_version", name="uq_news_sentiment_item_version"),
    )
    op.create_index("ix_news_sentiment_event", "news_sentiment_analyses", ["event_key"])
    op.create_index("ix_news_sentiment_analyzed", "news_sentiment_analyses", ["analyzed_at"])


def downgrade() -> None:
    op.drop_index("ix_news_sentiment_analyzed", table_name="news_sentiment_analyses")
    op.drop_index("ix_news_sentiment_event", table_name="news_sentiment_analyses")
    op.drop_table("news_sentiment_analyses")
