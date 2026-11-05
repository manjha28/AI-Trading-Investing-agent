"""Add persisted progress and active-run concurrency guard."""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0045_research_orchestration"
down_revision: Union[str, None] = "0044_sentiment_failures"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "instrument_research_runs",
        sa.Column("progress", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="[]"),
    )
    op.alter_column("instrument_research_runs", "progress", server_default=None)
    op.add_column(
        "instrument_research_runs",
        sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.alter_column("instrument_research_runs", "cancel_requested", server_default=None)
    op.drop_constraint(
        "ck_instrument_research_runs_status", "instrument_research_runs", type_="check"
    )
    op.create_check_constraint(
        "ck_instrument_research_runs_status",
        "instrument_research_runs",
        "status IN ('running', 'complete', 'partial', 'failed', 'cancelled')",
    )
    op.create_index(
        "uq_instrument_research_run_active",
        "instrument_research_runs",
        ["user_id", "instrument_id"],
        unique=True,
        postgresql_where=sa.text("status = 'running'"),
    )


def downgrade() -> None:
    op.drop_index("uq_instrument_research_run_active", table_name="instrument_research_runs")
    op.drop_constraint(
        "ck_instrument_research_runs_status", "instrument_research_runs", type_="check"
    )
    op.create_check_constraint(
        "ck_instrument_research_runs_status",
        "instrument_research_runs",
        "status IN ('running', 'complete', 'partial', 'failed')",
    )
    op.drop_column("instrument_research_runs", "progress")
    op.drop_column("instrument_research_runs", "cancel_requested")
