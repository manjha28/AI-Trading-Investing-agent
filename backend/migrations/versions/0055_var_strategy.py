"""VAR intraday futures research: versioned configs and hypothetical backtest runs.

Revision ID: 0055_var_strategy
Revises: 0054_job_queue

Additive only (two new tables). Downgrade drops them; they hold research records only, never real portfolio data.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0055_var_strategy"
down_revision: Union[str, None] = "0054_job_queue"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSON_DOCUMENT = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "var_config_versions",
        sa.Column("id", sa.Uuid(), nullable=False), sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False), sa.Column("config", JSON_DOCUMENT, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"), sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "version_no", name="uq_var_config_user_version"),
    )
    op.create_table(
        "var_backtest_runs",
        sa.Column("id", sa.Uuid(), nullable=False), sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=12), nullable=False), sa.Column("hypothetical", sa.Boolean(), nullable=False),
        sa.Column("config_snapshot", JSON_DOCUMENT, nullable=False), sa.Column("request", JSON_DOCUMENT, nullable=False),
        sa.Column("result", JSON_DOCUMENT, nullable=True), sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('queued','running','completed','failed','unavailable')", name="ck_var_backtest_status"),
        sa.CheckConstraint("hypothetical = true", name="ck_var_backtest_hypothetical"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"), sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_var_backtest_user_created", "var_backtest_runs", ["user_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_var_backtest_user_created", table_name="var_backtest_runs")
    op.drop_table("var_backtest_runs")
    op.drop_table("var_config_versions")
