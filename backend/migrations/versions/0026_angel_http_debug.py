"""Store sanitized provider HTTP diagnostics on sync attempts.

Revision ID: 0026_angel_http_debug
Revises: 0025_provider_enable_toggles
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0026_angel_http_debug"
down_revision: Union[str, None] = "0025_provider_enable_toggles"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "market_data_sync_attempts",
        sa.Column(
            "http_diagnostics",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("market_data_sync_attempts", "http_diagnostics")
