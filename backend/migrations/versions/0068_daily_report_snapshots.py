"""Stored daily research report.

Revision ID: 0068_daily_report_snapshots
Revises: 0067_nse_data_and_listings

Additive: one new table; nothing existing is changed.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0068_daily_report_snapshots"
down_revision: Union[str, None] = "0067_nse_data_and_listings"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSON_DOC = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "daily_report_snapshots",
        sa.Column("id", sa.Uuid(), nullable=False), sa.Column("user_id", sa.Uuid(), nullable=False), sa.Column("built_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("build_seconds", sa.Float(), nullable=False), sa.Column("etag", sa.String(64), nullable=False), sa.Column("payload", JSON_DOC, nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"), sa.PrimaryKeyConstraint("id"), sa.UniqueConstraint("user_id", name="uq_daily_report_snapshots_user"),
    )


def downgrade() -> None:
    op.drop_table("daily_report_snapshots")
