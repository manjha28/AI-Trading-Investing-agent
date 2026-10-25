"""Persist sanitized provider failure categories for market-data sync attempts.

Revision ID: 0024_sync_attempt_reason
Revises: 0023_md_provider_settings
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0024_sync_attempt_reason"
down_revision: Union[str, None] = "0023_md_provider_settings"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "market_data_sync_attempts",
        sa.Column("reason_category", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("market_data_sync_attempts", "reason_category")
