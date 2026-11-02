"""Add deterministic strategies, immutable versions and recorded signals.

Revision ID: 0033_strategy_signals
Revises: 0032_broker_position_metrics
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0033_strategy_signals"
down_revision: Union[str, None] = "0032_broker_position_metrics"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSON_DOCUMENT = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "strategies",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("instrument_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=False),
        sa.Column("status", sa.String(length=12), nullable=False),
        sa.Column("current_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('suspended', 'active', 'retired')",
            name="ck_strategies_status",
        ),
        sa.CheckConstraint("current_version >= 1", name="ck_strategies_current_version"),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "name", name="uq_strategies_user_name"),
    )
    op.create_index("ix_strategies_user_status", "strategies", ["user_id", "status"])
    op.create_table(
        "strategy_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("strategy_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("params", JSON_DOCUMENT, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("version >= 1", name="ck_strategy_versions_version"),
        sa.ForeignKeyConstraint(["strategy_id"], ["strategies.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "strategy_id", "version", name="uq_strategy_versions_strategy_version"
        ),
    )
    op.create_table(
        "strategy_signals",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("strategy_id", sa.Uuid(), nullable=False),
        sa.Column("strategy_version", sa.Integer(), nullable=False),
        sa.Column("instrument_id", sa.Uuid(), nullable=False),
        sa.Column("direction", sa.String(length=8), nullable=False),
        sa.Column("status", sa.String(length=12), nullable=False),
        sa.Column("rejection_reason", sa.String(length=64), nullable=True),
        sa.Column("bar_timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("trigger_price", sa.Numeric(precision=24, scale=8), nullable=False),
        sa.Column("rationale", sa.String(length=500), nullable=False),
        sa.Column("evidence", JSON_DOCUMENT, nullable=False),
        sa.Column("dedupe_key", sa.String(length=160), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "direction IN ('entry', 'exit')", name="ck_strategy_signals_direction"
        ),
        sa.CheckConstraint(
            "status IN ('active', 'rejected', 'expired')",
            name="ck_strategy_signals_status",
        ),
        sa.CheckConstraint(
            "(status = 'rejected' AND rejection_reason IS NOT NULL) OR "
            "(status != 'rejected' AND rejection_reason IS NULL)",
            name="ck_strategy_signals_rejection_reason",
        ),
        sa.CheckConstraint("trigger_price > 0", name="ck_strategy_signals_trigger_price"),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["strategy_id"], ["strategies.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("strategy_id", "dedupe_key", name="uq_strategy_signals_strategy_dedupe"),
    )
    op.create_index(
        "ix_strategy_signals_user_created", "strategy_signals", ["user_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_strategy_signals_user_created", table_name="strategy_signals")
    op.drop_table("strategy_signals")
    op.drop_table("strategy_versions")
    op.drop_index("ix_strategies_user_status", table_name="strategies")
    op.drop_table("strategies")
