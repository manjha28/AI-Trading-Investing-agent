"""Owner-set runtime switches that must survive restarts (first use: pausing the Delta public feed).

Revision ID: 0075_app_flags
Revises: 0074_delta_live_execution

Additive: one new table; nothing existing is changed.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0075_app_flags"
down_revision: Union[str, None] = "0074_delta_live_execution"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table("app_flags", sa.Column("key", sa.String(48), primary_key=True), sa.Column("value", sa.String(120), nullable=False),
                    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))


def downgrade() -> None:
    op.drop_table("app_flags")
