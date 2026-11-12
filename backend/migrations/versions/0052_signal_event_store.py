"""Append-only signal lifecycle events, scan rejections, nine-state signal lifecycle.

Revision ID: 0052_signal_event_store
Revises: 0051_derivatives_platform

Status mapping (existing intraday_signals rows):
  new -> ready, active -> active, expired -> expired, invalidated -> invalidated,
  suppressed -> NOT a lifecycle state: moved to scan_rejections (full original row kept in payload) and removed from intraday_signals.
Every surviving signal gets one backfilled genesis event (source 'migration_backfill', observed_data_time NULL: the original data time is unknown and is not invented).
Downgrade restores the old status set; states that did not exist before are mapped to the nearest legacy one (lossy, documented in the function).
"""
import json
from datetime import datetime, timezone
from typing import Sequence, Union
from decimal import Decimal
from uuid import UUID, uuid4

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0052_signal_event_store"
down_revision: Union[str, None] = "0051_derivatives_platform"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSON_DOCUMENT = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
STATES = ("forming", "ready", "active", "partially_realised", "target", "stopped", "invalidated", "expired", "cancelled")
OLD_STATES = ("new", "active", "expired", "invalidated", "suppressed")
EVENT_REASONS = ("signal_created", "confirmed_next_scan", "exit_condition", "opposite_signal", "time_expired", "owner_cancel",
                 "target_reached", "stop_hit", "partial_realisation", "conditions_complete", "backfill_genesis")
REJECTION_REASONS = ("insufficient_bars", "stale_data", "synthetic_data", "missing_data_limit", "stale_spot", "no_history", "other")
FORWARD = {"new": "ready", "active": "active", "expired": "expired", "invalidated": "invalidated"}
BACK = {"ready": "new", "forming": "new", "active": "active", "partially_realised": "active", "target": "expired", "stopped": "expired",
        "expired": "expired", "invalidated": "invalidated", "cancelled": "invalidated"}


def _in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def _rejection_code(reason: str | None) -> str:
    r = (reason or "").lower()
    if "spot" in r and "stale" in r:
        return "stale_spot"
    if "stale" in r:
        return "stale_data"
    if "quality is insufficient" in r:
        return "missing_data_limit"
    return "other"


def _json_safe(row: dict) -> dict:
    out = {}
    for k, v in row.items():
        out[k] = v.isoformat() if isinstance(v, datetime) else (str(v) if not isinstance(v, (str, int, float, bool, list, dict, type(None))) else v)
    return out


def _sig_table():
    U, DT, J = sa.Uuid(), sa.DateTime(timezone=True), JSON_DOCUMENT
    return sa.table(
        "intraday_signals", sa.column("id", U), sa.column("user_id", U), sa.column("strategy_id", U), sa.column("strategy_version", sa.Integer()),
        sa.column("instrument_id", U), sa.column("market", sa.String()), sa.column("direction", sa.String()), sa.column("status", sa.String()),
        sa.column("suppression_reason", sa.String()), sa.column("timeframe", sa.String()), sa.column("signal_time", DT), sa.column("reference_price", sa.Numeric(24, 8)),
        sa.column("indicators", J), sa.column("conditions_met", J), sa.column("conditions_unmet", J), sa.column("rationale", sa.String()), sa.column("data_freshness", J),
        sa.column("dedupe_key", sa.String()), sa.column("expires_at", DT), sa.column("created_at", DT), sa.column("updated_at", DT), sa.column("option", J))


def _rej_table():
    U, DT = sa.Uuid(), sa.DateTime(timezone=True)
    return sa.table("scan_rejections", sa.column("id", U), sa.column("user_id", U), sa.column("scan_run_id", U), sa.column("strategy_id", U), sa.column("strategy_version_id", U),
                    sa.column("instrument_id", U), sa.column("direction", sa.String()), sa.column("reason_code", sa.String()), sa.column("reason_detail", sa.String()),
                    sa.column("recorded_at", DT), sa.column("observed_data_time", DT), sa.column("payload", JSON_DOCUMENT))


def _evt_table():
    U, DT = sa.Uuid(), sa.DateTime(timezone=True)
    return sa.table("intraday_signal_events", sa.column("event_id", U), sa.column("user_id", U), sa.column("signal_id", U), sa.column("from_state", sa.String()),
                    sa.column("to_state", sa.String()), sa.column("event_time", DT), sa.column("observed_data_time", DT), sa.column("reason_code", sa.String()),
                    sa.column("reason_detail", sa.String()), sa.column("strategy_version_id", U), sa.column("source", sa.String()), sa.column("payload", JSON_DOCUMENT))


def upgrade() -> None:
    bind = op.get_bind()
    now = datetime.now(timezone.utc)
    op.create_table(
        "intraday_signal_events",
        sa.Column("event_id", sa.Uuid(), nullable=False), sa.Column("user_id", sa.Uuid(), nullable=False), sa.Column("signal_id", sa.Uuid(), nullable=False),
        sa.Column("from_state", sa.String(24), nullable=True), sa.Column("to_state", sa.String(24), nullable=False),
        sa.Column("event_time", sa.DateTime(timezone=True), nullable=False), sa.Column("observed_data_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reason_code", sa.String(32), nullable=False), sa.Column("reason_detail", sa.String(500), nullable=True),
        sa.Column("strategy_version_id", sa.Uuid(), nullable=False), sa.Column("source", sa.String(80), nullable=False), sa.Column("payload", JSON_DOCUMENT, nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["signal_id"], ["intraday_signals.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["strategy_version_id"], ["strategy_versions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("event_id"),
        sa.CheckConstraint(f"to_state IN ({_in(STATES)})", name="ck_intraday_signal_events_to_state"),
        sa.CheckConstraint(f"from_state IS NULL OR from_state IN ({_in(STATES)})", name="ck_intraday_signal_events_from_state"),
        sa.CheckConstraint(f"reason_code IN ({_in(EVENT_REASONS)})", name="ck_intraday_signal_events_reason"),
        sa.CheckConstraint("observed_data_time IS NOT NULL OR source = 'migration_backfill'", name="ck_intraday_signal_events_data_time"),
    )
    op.create_index("ix_intraday_signal_events_signal", "intraday_signal_events", ["signal_id", "event_time"])
    op.create_index("ix_intraday_signal_events_user_time", "intraday_signal_events", ["user_id", "event_time"])
    op.create_table(
        "scan_rejections",
        sa.Column("id", sa.Uuid(), nullable=False), sa.Column("user_id", sa.Uuid(), nullable=False), sa.Column("scan_run_id", sa.Uuid(), nullable=True),
        sa.Column("strategy_id", sa.Uuid(), nullable=False), sa.Column("strategy_version_id", sa.Uuid(), nullable=False), sa.Column("instrument_id", sa.Uuid(), nullable=False),
        sa.Column("direction", sa.String(8), nullable=True), sa.Column("reason_code", sa.String(32), nullable=False), sa.Column("reason_detail", sa.String(500), nullable=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False), sa.Column("observed_data_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("payload", JSON_DOCUMENT, nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["strategy_id"], ["strategies.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["strategy_version_id"], ["strategy_versions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(f"reason_code IN ({_in(REJECTION_REASONS)})", name="ck_scan_rejections_reason"),
        sa.CheckConstraint("direction IS NULL OR direction IN ('long', 'short')", name="ck_scan_rejections_direction"),
    )
    op.create_index("ix_scan_rejections_run", "scan_rejections", ["user_id", "scan_run_id"])
    op.create_index("ix_scan_rejections_user_time", "scan_rejections", ["user_id", "recorded_at"])

    # --- migrate existing signals ---
    op.drop_constraint("ck_intraday_signals_status", "intraday_signals", type_="check")
    op.alter_column("intraday_signals", "status", existing_type=sa.String(12), type_=sa.String(24), existing_nullable=False)
    sig = _sig_table()
    ver = sa.table("strategy_versions", sa.column("id", sa.Uuid()), sa.column("strategy_id", sa.Uuid()), sa.column("version", sa.Integer()))
    rej, evt = _rej_table(), _evt_table()
    vid = {(r.strategy_id, r.version): r.id for r in bind.execute(sa.select(ver.c.id, ver.c.strategy_id, ver.c.version))}
    rows = [dict(r._mapping) for r in bind.execute(sa.select(sig))]
    for r in rows:
        key = (r["strategy_id"], r["strategy_version"])
        if r["status"] == "suppressed":
            if key in vid:
                bind.execute(rej.insert().values(id=uuid4(), user_id=r["user_id"], scan_run_id=None, strategy_id=r["strategy_id"], strategy_version_id=vid[key],
                                                 instrument_id=r["instrument_id"], direction=r["direction"], reason_code=_rejection_code(r["suppression_reason"]),
                                                 reason_detail=r["suppression_reason"], recorded_at=r["created_at"], observed_data_time=r["signal_time"],
                                                 payload={"backfilled": True, "legacy_signal": _json_safe(r)}))
            bind.execute(sig.delete().where(sig.c.id == r["id"]))
            continue
        new_state = FORWARD[r["status"]]
        bind.execute(sig.update().where(sig.c.id == r["id"]).values(status=new_state))
        if key in vid:
            bind.execute(evt.insert().values(
                event_id=uuid4(), user_id=r["user_id"], signal_id=r["id"], from_state=None, to_state=new_state, event_time=now, observed_data_time=None,
                reason_code="backfill_genesis", reason_detail="Synthetic genesis event created by migration 0052; earlier history was not recorded.",
                strategy_version_id=vid[key], source="migration_backfill",
                payload={"backfilled": True, "legacy_status": r["status"], "signal_time": r["signal_time"].isoformat() if r["signal_time"] else None,
                         "original_created_at": r["created_at"].isoformat() if r["created_at"] else None,
                         "original_updated_at": r["updated_at"].isoformat() if r["updated_at"] else None}))
    op.create_check_constraint("ck_intraday_signals_status", "intraday_signals", f"status IN ({_in(STATES)})")

    if bind.dialect.name == "postgresql":  # belt and braces behind the ORM guard: no UPDATE of history, even via raw SQL
        op.execute("CREATE FUNCTION forbid_append_only_update() RETURNS trigger AS $$ BEGIN RAISE EXCEPTION '% is append-only', TG_TABLE_NAME; END; $$ LANGUAGE plpgsql")
        for table in ("intraday_signal_events", "scan_rejections"):
            op.execute(f"CREATE TRIGGER trg_{table}_no_update BEFORE UPDATE ON {table} FOR EACH ROW EXECUTE FUNCTION forbid_append_only_update()")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for table in ("intraday_signal_events", "scan_rejections"):
            op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_no_update ON {table}")
        op.execute("DROP FUNCTION IF EXISTS forbid_append_only_update()")
    op.drop_constraint("ck_intraday_signals_status", "intraday_signals", type_="check")
    sig = _sig_table()
    for new, old in BACK.items():  # lossy for states that did not exist before 0052 (see BACK)
        bind.execute(sig.update().where(sig.c.status == new).values(status=old))
    rej = _rej_table()
    for (payload,) in bind.execute(sa.select(rej.c.payload)):
        legacy = (payload if isinstance(payload, dict) else json.loads(payload)).get("legacy_signal") if payload else None
        if not legacy:
            continue  # rejections created after 0052 never existed as signals
        row = dict(legacy)
        for k in ("id", "user_id", "strategy_id", "instrument_id"):
            row[k] = UUID(row[k])
        row["reference_price"] = Decimal(row["reference_price"])
        for k in ("signal_time", "expires_at", "created_at", "updated_at"):
            row[k] = datetime.fromisoformat(row[k]) if row.get(k) else None
        bind.execute(sig.insert().values(**row))
    op.alter_column("intraday_signals", "status", existing_type=sa.String(24), type_=sa.String(12), existing_nullable=False)
    op.create_check_constraint("ck_intraday_signals_status", "intraday_signals", f"status IN ({_in(OLD_STATES)})")
    op.drop_index("ix_scan_rejections_user_time", table_name="scan_rejections")
    op.drop_index("ix_scan_rejections_run", table_name="scan_rejections")
    op.drop_table("scan_rejections")
    op.drop_index("ix_intraday_signal_events_user_time", table_name="intraday_signal_events")
    op.drop_index("ix_intraday_signal_events_signal", table_name="intraday_signal_events")
    op.drop_table("intraday_signal_events")
