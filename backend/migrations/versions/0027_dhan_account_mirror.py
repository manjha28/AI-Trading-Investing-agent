"""Persist Dhan holding identifiers and account funds.

Revision ID: 0027_dhan_account_mirror
Revises: 0026_angel_http_debug
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0027_dhan_account_mirror"
down_revision: Union[str, None] = "0026_angel_http_debug"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("broker_mirror", sa.Column("security_id", sa.String(length=64)))
    op.add_column("broker_mirror", sa.Column("isin", sa.String(length=16)))
    op.create_table(
        "broker_account_mirror",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("broker", sa.String(length=32), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False, server_default="INR"),
        sa.Column("available_balance", sa.Numeric(precision=24, scale=2), nullable=False),
        sa.Column("sod_limit", sa.Numeric(precision=24, scale=2), nullable=True),
        sa.Column("collateral_amount", sa.Numeric(precision=24, scale=2), nullable=True),
        sa.Column("receivable_amount", sa.Numeric(precision=24, scale=2), nullable=True),
        sa.Column("utilized_amount", sa.Numeric(precision=24, scale=2), nullable=True),
        sa.Column("blocked_payout_amount", sa.Numeric(precision=24, scale=2), nullable=True),
        sa.Column("withdrawable_balance", sa.Numeric(precision=24, scale=2), nullable=True),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id", "broker", name="uq_broker_account_mirror_user_broker"
        ),
    )
    op.create_index(
        "ix_broker_account_mirror_user_broker",
        "broker_account_mirror",
        ["user_id", "broker"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_broker_account_mirror_user_broker",
        table_name="broker_account_mirror",
    )
    op.drop_table("broker_account_mirror")
    op.drop_column("broker_mirror", "isin")
    op.drop_column("broker_mirror", "security_id")
