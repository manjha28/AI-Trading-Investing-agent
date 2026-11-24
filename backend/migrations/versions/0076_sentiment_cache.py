"""Precomputed news-sentiment list payloads, written by the sentiment job.

Revision ID: 0076_sentiment_cache
Revises: 0075_app_flags

Additive: one new table; nothing existing is changed.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.models import JSON_DOCUMENT

revision: str = "0076_sentiment_cache"
down_revision: Union[str, None] = "0075_app_flags"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table("sentiment_aggregate_cache", sa.Column("key", sa.String(64), primary_key=True), sa.Column("payload", JSON_DOCUMENT, nullable=False),
                    sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False))


def downgrade() -> None:
    op.drop_table("sentiment_aggregate_cache")
