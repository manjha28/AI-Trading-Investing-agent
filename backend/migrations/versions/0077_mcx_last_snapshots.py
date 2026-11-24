"""Last snapshot per MCX contract (shown while the market is closed).

Revision ID: 0077_mcx_last_snapshots
Revises: 0076_sentiment_cache

Additive: one new table; nothing existing is changed.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.models import JSON_DOCUMENT

revision: str = "0077_mcx_last_snapshots"
down_revision: Union[str, None] = "0076_sentiment_cache"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table("mcx_last_snapshots", sa.Column("security_id", sa.String(32), primary_key=True), sa.Column("symbol", sa.String(32), nullable=False),
                    sa.Column("payload", JSON_DOCUMENT, nullable=False), sa.Column("session_date", sa.Date(), nullable=True),
                    sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False))


def downgrade() -> None:
    op.drop_table("mcx_last_snapshots")
