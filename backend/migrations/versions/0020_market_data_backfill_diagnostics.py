"""Add market-data sync diagnostics and forbid synthetic persisted prices.

Revision ID: 0020_market_data_backfill
Revises: 0019_research_pipeline
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0020_market_data_backfill"
down_revision: Union[str, None] = "0019_research_pipeline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("market_data_syncs", sa.Column("requested_start", sa.DateTime(timezone=True)))
    op.add_column("market_data_syncs", sa.Column("requested_end", sa.DateTime(timezone=True)))
    op.add_column("market_data_syncs", sa.Column("requested_limit", sa.Integer()))
    op.add_column(
        "market_data_syncs",
        sa.Column("rows_written", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("market_data_syncs", sa.Column("response_status", sa.Integer()))
    op.add_column("market_data_syncs", sa.Column("safe_reason", sa.String(length=200)))

    op.create_table(
        "market_data_sync_attempts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("instrument_id", sa.Uuid(), nullable=False),
        sa.Column("interval", sa.String(length=8), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("requested_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("requested_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("requested_limit", sa.Integer(), nullable=False),
        sa.Column("response_status", sa.Integer()),
        sa.Column("rows_returned", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rows_written", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.String(length=200)),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "interval IN ('1d', '1h', '15m', '1m')",
            name="ck_market_sync_attempt_interval",
        ),
        sa.CheckConstraint(
            "outcome IN ('success', 'unavailable', 'partial', 'unchanged')",
            name="ck_market_sync_attempt_outcome",
        ),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_market_sync_attempt_instrument_attempted",
        "market_data_sync_attempts",
        ["instrument_id", "attempted_at"],
    )

    op.execute("DELETE FROM market_bars WHERE is_synthetic = true")
    op.execute("DELETE FROM market_quotes WHERE is_synthetic = true")
    with op.batch_alter_table("market_bars") as batch:
        batch.create_check_constraint("ck_market_bars_real_only", "is_synthetic = false")
    with op.batch_alter_table("market_quotes") as batch:
        batch.create_check_constraint("ck_market_quotes_real_only", "is_synthetic = false")


def downgrade() -> None:
    with op.batch_alter_table("market_quotes") as batch:
        batch.drop_constraint("ck_market_quotes_real_only", type_="check")
    with op.batch_alter_table("market_bars") as batch:
        batch.drop_constraint("ck_market_bars_real_only", type_="check")
    op.drop_index(
        "ix_market_sync_attempt_instrument_attempted",
        table_name="market_data_sync_attempts",
    )
    op.drop_table("market_data_sync_attempts")
    op.drop_column("market_data_syncs", "safe_reason")
    op.drop_column("market_data_syncs", "response_status")
    op.drop_column("market_data_syncs", "rows_written")
    op.drop_column("market_data_syncs", "requested_limit")
    op.drop_column("market_data_syncs", "requested_end")
    op.drop_column("market_data_syncs", "requested_start")
