"""Persist per-user market-data provider enablement.

Revision ID: 0025_provider_enable_toggles
Revises: 0024_sync_attempt_reason
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0025_provider_enable_toggles"
down_revision: Union[str, None] = "0024_sync_attempt_reason"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "user_market_data_settings",
        sa.Column(
            "disabled_providers",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("user_market_data_settings", "disabled_providers")
