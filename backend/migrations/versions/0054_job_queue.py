"""Durable Postgres job queue, scanner settings/schedules and scan-run history.

Revision ID: 0054_job_queue
Revises: 0053_provider_unserved

Additive only (four new tables). Downgrade drops them; they hold operational records only.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0054_job_queue"
down_revision: Union[str, None] = "0053_provider_unserved"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSON_DOCUMENT = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("id", sa.Uuid(), nullable=False), sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("job_type", sa.String(length=32), nullable=False), sa.Column("payload", JSON_DOCUMENT, nullable=False),
        sa.Column("status", sa.String(length=12), nullable=False), sa.Column("attempts", sa.Integer(), nullable=False), sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("locked_by", sa.String(length=96), nullable=True), sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False), sa.Column("progress", JSON_DOCUMENT, nullable=False),
        sa.Column("result", JSON_DOCUMENT, nullable=True), sa.Column("error", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=160), nullable=True), sa.Column("concurrency_key", sa.String(length=160), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('queued','running','succeeded','failed','cancelled')", name="ck_jobs_status"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"), sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "idempotency_key", name="uq_jobs_idempotency"),
    )
    op.create_index("ix_jobs_claim", "jobs", ["status", "next_attempt_at"])
    op.create_index("uq_jobs_running_concurrency", "jobs", ["concurrency_key"], unique=True, postgresql_where=sa.text("status = 'running'"))
    op.create_table(
        "scanner_settings",
        sa.Column("user_id", sa.Uuid(), nullable=False), sa.Column("scans_enabled", sa.Boolean(), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"), sa.PrimaryKeyConstraint("user_id"),
    )
    op.create_table(
        "scanner_schedules",
        sa.Column("id", sa.Uuid(), nullable=False), sa.Column("user_id", sa.Uuid(), nullable=False), sa.Column("strategy_id", sa.Uuid(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False), sa.Column("cadence_minutes", sa.Integer(), nullable=False),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True), sa.Column("last_enqueued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_job_id", sa.Uuid(), nullable=True), sa.Column("last_skip_reason", sa.String(length=120), nullable=True), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"), sa.ForeignKeyConstraint(["strategy_id"], ["strategies.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"), sa.UniqueConstraint("user_id", "strategy_id", name="uq_scanner_schedule_strategy"),
    )
    op.create_table(
        "scan_runs",
        sa.Column("id", sa.Uuid(), nullable=False), sa.Column("scan_run_id", sa.Uuid(), nullable=False), sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("strategy_id", sa.Uuid(), nullable=False), sa.Column("job_id", sa.Uuid(), nullable=True), sa.Column("trigger", sa.String(length=12), nullable=False),
        sa.Column("status", sa.String(length=12), nullable=False), sa.Column("candle_close", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False), sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("pairs_scanned", sa.Integer(), nullable=False), sa.Column("instrument_ids", JSON_DOCUMENT, nullable=False), sa.Column("new_signals", sa.Integer(), nullable=False),
        sa.Column("suppressed", sa.Integer(), nullable=False), sa.Column("by_reason", JSON_DOCUMENT, nullable=False), sa.Column("errors", JSON_DOCUMENT, nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"), sa.ForeignKeyConstraint(["strategy_id"], ["strategies.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_scan_runs_group", "scan_runs", ["user_id", "scan_run_id"])
    op.create_index("ix_scan_runs_started", "scan_runs", ["user_id", "started_at"])


def downgrade() -> None:
    op.drop_index("ix_scan_runs_started", table_name="scan_runs")
    op.drop_index("ix_scan_runs_group", table_name="scan_runs")
    op.drop_table("scan_runs")
    op.drop_table("scanner_schedules")
    op.drop_table("scanner_settings")
    op.drop_index("uq_jobs_running_concurrency", table_name="jobs")
    op.drop_index("ix_jobs_claim", table_name="jobs")
    op.drop_table("jobs")
