"""Add encrypted TOTP credentials and verification state.

Revision ID: 0013_app_two_factor
Revises: 0012_email_verification
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0013_app_two_factor"
down_revision: Union[str, None] = "0012_email_verification"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("two_factor_secret_encrypted", sa.Text(), nullable=True))
    op.add_column(
        "users",
        sa.Column("two_factor_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("users", sa.Column("two_factor_last_counter", sa.BigInteger(), nullable=True))
    op.add_column(
        "users",
        sa.Column("two_factor_failed_attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "users",
        sa.Column("two_factor_locked_until", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "two_factor_locked_until")
    op.drop_column("users", "two_factor_failed_attempts")
    op.drop_column("users", "two_factor_last_counter")
    op.drop_column("users", "two_factor_enabled")
    op.drop_column("users", "two_factor_secret_encrypted")
