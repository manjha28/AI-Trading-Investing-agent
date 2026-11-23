"""Instrument kind (stock or fund) stored on the instrument row.

Revision ID: 0070_instrument_kind
Revises: 0069_stock_verdicts

Additive: one nullable column; nothing existing is changed. Filled when the Dhan master is synced (app.services.instrument_kind.sync_kinds).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0070_instrument_kind"
down_revision: Union[str, None] = "0069_stock_verdicts"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("instruments", sa.Column("instrument_kind", sa.String(8), nullable=True))


def downgrade() -> None:
    op.drop_column("instruments", "instrument_kind")
