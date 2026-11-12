"""Provider-unserved session windows (structural gaps, recorded apart from fetch failures).

Revision ID: 0053_provider_unserved
Revises: 0052_signal_event_store

Additive only: one new table, no existing column touched. Downgrade drops the new table (it holds derived records only).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0053_provider_unserved"
down_revision: Union[str, None] = "0052_signal_event_store"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSON_DOCUMENT = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "provider_unserved_windows",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("instrument_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("interval", sa.String(length=8), nullable=False),
        sa.Column("start_minute", sa.Integer(), nullable=False),
        sa.Column("end_minute", sa.Integer(), nullable=False),
        sa.Column("reason_code", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=12), nullable=False),
        sa.Column("sessions_examined", sa.Integer(), nullable=False),
        sa.Column("sessions_matching", sa.Integer(), nullable=False),
        sa.Column("evidence", JSON_DOCUMENT, nullable=False),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_confirmed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("instrument_id", "provider", "interval", "start_minute", "end_minute", name="uq_provider_unserved_window"),
    )
    op.create_index("ix_provider_unserved_lookup", "provider_unserved_windows", ["instrument_id", "provider", "interval", "status"])


def downgrade() -> None:
    op.drop_index("ix_provider_unserved_lookup", table_name="provider_unserved_windows")
    op.drop_table("provider_unserved_windows")
