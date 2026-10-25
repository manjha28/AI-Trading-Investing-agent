"""Retain Dhan voucher IDs for ledger line-level idempotency.

Revision ID: 0029_dhan_ledger_line_key
Revises: 0028_dhan_transaction_mirror
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0029_dhan_ledger_line_key"
down_revision: Union[str, None] = "0028_dhan_transaction_mirror"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "dhan_ledger_mirror",
        sa.Column("provider_entry_id", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("dhan_ledger_mirror", "provider_entry_id")
