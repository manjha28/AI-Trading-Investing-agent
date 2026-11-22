"""Shadow record of the Nexus v2 Gate -> Quality -> Timing decision, stored beside v1.

Revision ID: 0064_pipeline_v2_shadow
Revises: 0063_fundamental_snapshots

Additive: one new table; nothing existing is changed.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0064_pipeline_v2_shadow"
down_revision: Union[str, None] = "0063_fundamental_snapshots"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSON_DOC = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "pipeline_v2_shadow",
        sa.Column("id", sa.Uuid(), nullable=False), sa.Column("instrument_id", sa.Uuid(), nullable=False), sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("brief_id", sa.Uuid(), nullable=True), sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False), sa.Column("as_of_candle", sa.Date(), nullable=True),
        sa.Column("v2_buy_horizons", sa.Integer(), nullable=False), sa.Column("relaxation_dependent_ladders", sa.Integer(), nullable=False), sa.Column("payload", JSON_DOC, nullable=False),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"], ondelete="CASCADE"), sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"), sa.PrimaryKeyConstraint("id"))
    op.create_index("ix_pipeline_v2_shadow_instrument_time", "pipeline_v2_shadow", ["instrument_id", "computed_at"])
    op.create_index("ix_pipeline_v2_shadow_user_time", "pipeline_v2_shadow", ["user_id", "computed_at"])


def downgrade() -> None:
    op.drop_index("ix_pipeline_v2_shadow_user_time", table_name="pipeline_v2_shadow")
    op.drop_index("ix_pipeline_v2_shadow_instrument_time", table_name="pipeline_v2_shadow")
    op.drop_table("pipeline_v2_shadow")
