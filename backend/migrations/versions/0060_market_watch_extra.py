"""Market watch master-list additions.

Revision ID: 0060_market_watch_extra
Revises: 0059_crypto_delta

Additive: one new per-user table.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0060_market_watch_extra"
down_revision: Union[str, None] = "0059_crypto_delta"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "market_watch_extras",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("instrument_id", sa.Uuid(), sa.ForeignKey("instruments.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "instrument_id", name="uq_market_watch_extra"),
    )
    op.create_index("ix_market_watch_extras_user", "market_watch_extras", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_market_watch_extras_user", table_name="market_watch_extras")
    op.drop_table("market_watch_extras")
