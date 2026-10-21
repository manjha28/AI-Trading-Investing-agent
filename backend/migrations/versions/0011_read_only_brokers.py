"""Add read-only broker connections and account mirrors.

Revision ID: 0011_read_only_brokers
Revises: 0010_free_market_data_and_relink
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0011_read_only_brokers"
down_revision: Union[str, None] = "0010_free_market_data_and_relink"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("email_verified", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "user_sessions",
        sa.Column("two_factor_verified", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_table(
        "broker_credentials",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("broker", sa.String(length=32), nullable=False),
        sa.Column("encrypted_tokens", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("connected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "broker", name="uq_broker_credentials_user_broker"),
    )
    op.create_index("ix_broker_credentials_user_id", "broker_credentials", ["user_id"])
    op.create_table(
        "broker_oauth_states",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("broker", sa.String(length=32), nullable=False),
        sa.Column("state_hash", sa.String(length=64), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("state_hash"),
    )
    op.create_index("ix_broker_oauth_states_user_id", "broker_oauth_states", ["user_id"])
    op.create_index("ix_broker_oauth_states_expires_at", "broker_oauth_states", ["expires_at"])
    op.create_table(
        "broker_mirror",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("broker", sa.String(length=32), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("exchange", sa.String(length=32), nullable=False),
        sa.Column("product", sa.String(length=32), nullable=False),
        sa.Column("quantity", sa.Numeric(24, 8), nullable=False),
        sa.Column("average_price", sa.Numeric(24, 8), nullable=True),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("kind IN ('holding', 'position')", name="ck_broker_mirror_kind"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id",
            "broker",
            "kind",
            "symbol",
            "exchange",
            "product",
            name="uq_broker_mirror_position",
        ),
    )
    op.create_index("ix_broker_mirror_user_broker", "broker_mirror", ["user_id", "broker"])


def downgrade() -> None:
    op.drop_index("ix_broker_mirror_user_broker", table_name="broker_mirror")
    op.drop_table("broker_mirror")
    op.drop_index("ix_broker_oauth_states_expires_at", table_name="broker_oauth_states")
    op.drop_index("ix_broker_oauth_states_user_id", table_name="broker_oauth_states")
    op.drop_table("broker_oauth_states")
    op.drop_index("ix_broker_credentials_user_id", table_name="broker_credentials")
    op.drop_table("broker_credentials")
    op.drop_column("user_sessions", "two_factor_verified")
    op.drop_column("users", "email_verified")
