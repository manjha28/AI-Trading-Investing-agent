"""Nightly multibagger-engine results.

Revision ID: 0066_multibagger_assessments
Revises: 0065_price_crosscheck

Additive: one new table; nothing existing is changed.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0066_multibagger_assessments"
down_revision: Union[str, None] = "0065_price_crosscheck"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSON_DOC = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "multibagger_assessments",
        sa.Column("id", sa.Uuid(), nullable=False), sa.Column("instrument_id", sa.Uuid(), nullable=False), sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("as_of", sa.Date(), nullable=False), sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False), sa.Column("class", sa.String(32), nullable=False),
        sa.Column("q", sa.Float(), nullable=True), sa.Column("g", sa.Float(), nullable=True), sa.Column("v", sa.Float(), nullable=True), sa.Column("t", sa.Float(), nullable=True),
        sa.Column("x", sa.Float(), nullable=True), sa.Column("rank", sa.Float(), nullable=True), sa.Column("checks", JSON_DOC, nullable=False), sa.Column("model_version", sa.String(48), nullable=False),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"], ondelete="CASCADE"), sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"), sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("instrument_id", "user_id", "as_of", name="uq_multibagger_instrument_user_day"),
    )
    op.create_index("ix_multibagger_user_day", "multibagger_assessments", ["user_id", "as_of"])


def downgrade() -> None:
    op.drop_index("ix_multibagger_user_day", table_name="multibagger_assessments")
    op.drop_table("multibagger_assessments")
