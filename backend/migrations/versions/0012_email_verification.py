"""Add single-use email verification tokens.

Revision ID: 0012_email_verification
Revises: 0011_read_only_brokers
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0012_email_verification"
down_revision: Union[str, None] = "0011_read_only_brokers"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "email_verification_tokens",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index(
        "ix_email_verification_tokens_user_created",
        "email_verification_tokens",
        ["user_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_email_verification_tokens_user_created",
        table_name="email_verification_tokens",
    )
    op.drop_table("email_verification_tokens")
