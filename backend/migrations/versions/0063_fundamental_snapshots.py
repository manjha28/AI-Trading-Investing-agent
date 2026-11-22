"""Dated, append-only fundamental_snapshots and shareholding_snapshots, plus governance_events (Phase 6 step 1: the future point-in-time store).

Revision ID: 0063_fundamental_snapshots
Revises: 0062_fundamentals_fetch_log

Additive: three new tables; nothing existing is changed.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0063_fundamental_snapshots"
down_revision: Union[str, None] = "0062_fundamentals_fetch_log"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSON_DOC = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "fundamental_snapshots",
        sa.Column("id", sa.Uuid(), nullable=False), sa.Column("instrument_id", sa.Uuid(), nullable=False),
        sa.Column("captured_on", sa.Date(), nullable=False), sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False), sa.Column("payload", JSON_DOC, nullable=False),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"], ondelete="CASCADE"), sa.PrimaryKeyConstraint("id"))
    op.create_index("ix_fundamental_snapshots_instrument_date", "fundamental_snapshots", ["instrument_id", "captured_on"])
    op.create_table(
        "shareholding_snapshots",
        sa.Column("id", sa.Uuid(), nullable=False), sa.Column("instrument_id", sa.Uuid(), nullable=False),
        sa.Column("captured_on", sa.Date(), nullable=False), sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("promoter_pct", sa.Numeric(9, 4), nullable=True), sa.Column("fii_pct", sa.Numeric(9, 4), nullable=True), sa.Column("dii_pct", sa.Numeric(9, 4), nullable=True),
        sa.Column("public_pct", sa.Numeric(9, 4), nullable=True), sa.Column("pledged_pct_of_promoter", sa.Numeric(9, 4), nullable=True),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"], ondelete="CASCADE"), sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("instrument_id", "captured_on", "source", name="uq_shareholding_snapshot_day"))
    op.create_table(
        "governance_events",
        sa.Column("id", sa.Uuid(), nullable=False), sa.Column("instrument_id", sa.Uuid(), nullable=False), sa.Column("event_date", sa.Date(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False), sa.Column("source", sa.String(length=64), nullable=False), sa.Column("url", sa.String(length=512), nullable=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"], ondelete="CASCADE"), sa.PrimaryKeyConstraint("id"))
    op.create_index("ix_governance_events_instrument_date", "governance_events", ["instrument_id", "event_date"])


def downgrade() -> None:
    op.drop_index("ix_governance_events_instrument_date", table_name="governance_events")
    op.drop_table("governance_events")
    op.drop_table("shareholding_snapshots")
    op.drop_index("ix_fundamental_snapshots_instrument_date", table_name="fundamental_snapshots")
    op.drop_table("fundamental_snapshots")
