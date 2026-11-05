"""Track latest poll health and coverage counters by news source."""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0042_news_source_health"
down_revision: Union[str, None] = "0041_news_filings"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "news_source_polls",
        sa.Column("source", sa.String(32), primary_key=True),
        sa.Column("last_attempted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_successful_at", sa.DateTime(timezone=True)),
        sa.Column("last_article_at", sa.DateTime(timezone=True)),
        sa.Column("articles_discovered", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("articles_inserted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("duplicates", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failure_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error_category", sa.String(64)),
        sa.Column("rate_limited", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_table("news_source_polls")
