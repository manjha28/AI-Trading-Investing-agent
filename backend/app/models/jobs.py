"""Durable job queue, scanner schedule and scan-run history (migration 0054)."""
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models import JSON_DOCUMENT

JOB_STATUSES = ("queued", "running", "succeeded", "failed", "cancelled")


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint("status IN ('queued','running','succeeded','failed','cancelled')", name="ck_jobs_status"),
        UniqueConstraint("user_id", "idempotency_key", name="uq_jobs_idempotency"),
        Index("ix_jobs_claim", "status", "next_attempt_at"),
        # At most one RUNNING job per concurrency key: the database, not application code, prevents overlapping runs.
        Index("uq_jobs_running_concurrency", "concurrency_key", unique=True, postgresql_where=text("status = 'running'"), sqlite_where=text("status = 'running'")),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    job_type: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON_DOCUMENT, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="queued")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    locked_by: Mapped[str | None] = mapped_column(String(96))
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    progress: Mapped[dict] = mapped_column(JSON_DOCUMENT, nullable=False, default=dict)
    result: Mapped[dict | None] = mapped_column(JSON_DOCUMENT)
    error: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str | None] = mapped_column(String(160))
    concurrency_key: Mapped[str | None] = mapped_column(String(160))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ScannerSettings(Base):
    """Per-owner global scanner kill switch. Off by default: nothing scans on a schedule until the owner turns it on."""
    __tablename__ = "scanner_settings"

    user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    scans_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ScannerSchedule(Base):
    __tablename__ = "scanner_schedules"
    __table_args__ = (UniqueConstraint("user_id", "strategy_id", name="uq_scanner_schedule_strategy"),)

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    strategy_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("strategies.id", ondelete="CASCADE"), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    cadence_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_enqueued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_job_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    last_skip_reason: Mapped[str | None] = mapped_column(String(120))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ScanRun(Base):
    """One strategy's pass inside a scan run (``scan_run_id`` groups the passes of one manual or scheduled scan)."""
    __tablename__ = "scan_runs"
    __table_args__ = (Index("ix_scan_runs_group", "user_id", "scan_run_id"), Index("ix_scan_runs_started", "user_id", "started_at"))

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    scan_run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    strategy_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("strategies.id", ondelete="CASCADE"), nullable=False)
    job_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    trigger: Mapped[str] = mapped_column(String(12), nullable=False)        # manual | schedule
    status: Mapped[str] = mapped_column(String(12), nullable=False)         # running | succeeded | failed
    candle_close: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pairs_scanned: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    instrument_ids: Mapped[list] = mapped_column(JSON_DOCUMENT, nullable=False, default=list)
    new_signals: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    suppressed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    by_reason: Mapped[dict] = mapped_column(JSON_DOCUMENT, nullable=False, default=dict)
    errors: Mapped[list] = mapped_column(JSON_DOCUMENT, nullable=False, default=list)
