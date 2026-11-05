"""Track sentiment classification failures and bounded retry times."""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0044_sentiment_failures"
down_revision: Union[str, None] = "0043_news_sentiment"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "news_sentiment_failures",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("news_item_id", sa.Uuid(), nullable=False),
        sa.Column("classifier_version", sa.String(32), nullable=False),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("retry_after", sa.DateTime(timezone=True), nullable=False),
        sa.Column("error_category", sa.String(64), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="1"),
        sa.ForeignKeyConstraint(["news_item_id"], ["news_items.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("news_item_id", "classifier_version", name="uq_news_sentiment_failure_item_version"),
    )


def downgrade() -> None:
    op.drop_table("news_sentiment_failures")
