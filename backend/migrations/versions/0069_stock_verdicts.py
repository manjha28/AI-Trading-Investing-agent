"""Dated stock verdicts.

Revision ID: 0069_stock_verdicts
Revises: 0068_daily_report_snapshots

Additive: one new table; nothing existing is changed.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0069_stock_verdicts"
down_revision: Union[str, None] = "0068_daily_report_snapshots"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSON_DOC = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "stock_verdicts",
        sa.Column("id", sa.Uuid(), nullable=False), sa.Column("instrument_id", sa.Uuid(), nullable=False), sa.Column("user_id", sa.Uuid(), nullable=False), sa.Column("as_of", sa.Date(), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False), sa.Column("horizon", sa.String(12), nullable=False), sa.Column("verdict", sa.String(16), nullable=False),
        sa.Column("evidence_score", sa.Float(), nullable=True), sa.Column("calibrated_confidence", sa.Float(), nullable=True), sa.Column("lens", JSON_DOC, nullable=False), sa.Column("reasons", JSON_DOC, nullable=False),
        sa.Column("model_version", sa.String(48), nullable=False),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"], ondelete="CASCADE"), sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"), sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("instrument_id", "user_id", "as_of", "horizon", name="uq_stock_verdict_day"),
    )
    op.create_index("ix_stock_verdicts_user_day", "stock_verdicts", ["user_id", "as_of"])


def downgrade() -> None:
    op.drop_index("ix_stock_verdicts_user_day", table_name="stock_verdicts")
    op.drop_table("stock_verdicts")
