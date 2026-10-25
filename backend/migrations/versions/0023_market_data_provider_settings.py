"""Persist user market-data provider selection and Angel One session tokens.

Revision ID: 0023_md_provider_settings
Revises: 0022_market_data_intervals
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0023_md_provider_settings"
down_revision: Union[str, None] = "0022_market_data_intervals"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "user_market_data_settings",
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("selected_provider", sa.String(length=32), nullable=True),
        sa.Column("encrypted_angel_tokens", sa.Text(), nullable=True),
        sa.Column("angel_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("angel_connected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("user_market_data_settings")
