"""Record the currency used by each journal trade.

Revision ID: 0005_journal_currency
Revises: 0004_journal_trades
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0005_journal_currency"
down_revision: Union[str, None] = "0004_journal_trades"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "journal_trades",
        sa.Column("currency", sa.String(length=3), nullable=False, server_default="USD"),
    )
    op.create_check_constraint(
        "ck_journal_currency", "journal_trades", "currency IN ('USD', 'INR')"
    )
    op.alter_column("journal_trades", "currency", server_default=None)


def downgrade() -> None:
    op.drop_constraint("ck_journal_currency", "journal_trades", type_="check")
    op.drop_column("journal_trades", "currency")