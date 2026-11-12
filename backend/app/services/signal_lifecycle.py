"""The ONLY place signal state is written. Every change appends an IntradaySignalEvent (INSERT-only) and updates the signal's current-state column.

``event_time`` is processing time (when we recorded it); ``observed_data_time`` is the market timestamp of the data that caused the change.
Callers must supply both explicitly: neither is ever defaulted from the other.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import inspect as sa_inspect, select
from sqlalchemy.orm import Session

from app.models.strategies import (EVENT_REASON_CODES, REJECTION_REASON_CODES, SIGNAL_STATES, IntradaySignal, IntradaySignalEvent, ScanRejection,
                                   StrategyVersion)

log = logging.getLogger("app.signal_lifecycle")

TERMINAL = frozenset({"target", "stopped", "invalidated", "expired", "cancelled"})
OPEN_STATES = tuple(s for s in SIGNAL_STATES if s not in TERMINAL)  # forming, ready, active, partially_realised
CREATION_STATES = frozenset({"forming", "ready"})
ALLOWED: dict[str, frozenset[str]] = {
    "forming": frozenset({"ready", "expired", "invalidated"}),
    "ready": frozenset({"active", "expired", "invalidated"}),
    "active": frozenset({"partially_realised", "target", "stopped", "invalidated", "expired"}),
    "partially_realised": frozenset({"target", "stopped", "invalidated", "expired"}),
    **{s: frozenset() for s in TERMINAL},
}
# Explicit owner cancellation is allowed from any NON-terminal state (terminal states, including cancelled, stay terminal).


class InvalidTransition(ValueError):
    """Raised (and logged) for any transition outside the state machine."""


def _aware(value: datetime | None, name: str) -> datetime:
    if value is None:
        raise InvalidTransition(f"{name} is required")
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def version_id_for(db: Session, signal: IntradaySignal) -> UUID:
    vid = db.scalar(select(StrategyVersion.id).where(StrategyVersion.strategy_id == signal.strategy_id, StrategyVersion.version == signal.strategy_version))
    if vid is None:
        raise InvalidTransition(f"strategy version {signal.strategy_version} not found for signal {signal.id}")
    return vid


def _check(to_state: str, from_state: str | None, reason_code: str) -> None:
    if to_state not in SIGNAL_STATES:
        raise InvalidTransition(f"unknown state {to_state!r}")
    if reason_code not in EVENT_REASON_CODES or reason_code == "backfill_genesis":
        raise InvalidTransition(f"reason_code {reason_code!r} is not allowed for a live transition")
    if from_state is None:
        if to_state not in CREATION_STATES:
            raise InvalidTransition(f"a signal can only be created as forming or ready, not {to_state}")
        return
    if from_state not in SIGNAL_STATES:
        raise InvalidTransition(f"unknown current state {from_state!r}")
    if to_state == "cancelled":
        if from_state in TERMINAL:
            raise InvalidTransition(f"{from_state} is terminal; cannot cancel")
        if reason_code != "owner_cancel":
            raise InvalidTransition("cancellation requires reason_code owner_cancel")
        return
    if reason_code == "owner_cancel":
        raise InvalidTransition("owner_cancel is only valid for a transition to cancelled")
    if to_state not in ALLOWED[from_state]:
        raise InvalidTransition(f"{from_state} -> {to_state} is not an allowed transition")


def create_signal(db: Session, row: IntradaySignal, *, state: str, observed_data_time: datetime, event_time: datetime, source: str,
                  strategy_version_id: UUID, reason_detail: str | None = None, payload: dict[str, Any] | None = None) -> IntradaySignalEvent:
    """Persist a NEW signal in ``state`` (forming/ready) together with its creation event."""
    try:
        _check(state, None, "signal_created")
        obs, ev_time = _aware(observed_data_time, "observed_data_time"), _aware(event_time, "event_time")
    except InvalidTransition:
        log.error("invalid signal creation state=%s source=%s", state, source)
        raise
    row.status, row.updated_at = state, ev_time
    db.add(row)
    db.flush()
    ev = IntradaySignalEvent(event_id=uuid4(), user_id=row.user_id, signal_id=row.id, from_state=None, to_state=state, event_time=ev_time, observed_data_time=obs,
                             reason_code="signal_created", reason_detail=reason_detail, strategy_version_id=strategy_version_id, source=source, payload=payload or {})
    db.add(ev)
    return ev


def transition_signal(db: Session, signal: IntradaySignal, to_state: str, *, reason_code: str, observed_data_time: datetime, event_time: datetime, source: str,
                      reason_detail: str | None = None, payload: dict[str, Any] | None = None, strategy_version_id: UUID | None = None) -> IntradaySignalEvent:
    from_state = signal.status
    try:
        _check(to_state, from_state, reason_code)
        obs, ev_time = _aware(observed_data_time, "observed_data_time"), _aware(event_time, "event_time")
        vid = strategy_version_id or version_id_for(db, signal)
    except InvalidTransition as exc:
        log.error("invalid signal transition signal=%s %s -> %s reason=%s source=%s: %s", signal.id, from_state, to_state, reason_code, source, exc)
        raise
    sa_inspect(signal).info["lifecycle_write"] = True
    signal.status, signal.updated_at = to_state, ev_time
    ev = IntradaySignalEvent(event_id=uuid4(), user_id=signal.user_id, signal_id=signal.id, from_state=from_state, to_state=to_state, event_time=ev_time,
                             observed_data_time=obs, reason_code=reason_code, reason_detail=reason_detail, strategy_version_id=vid, source=source, payload=payload or {})
    db.add(ev)
    return ev


def last_observed_data_time(db: Session, signal_id: UUID) -> datetime | None:
    """Most recent data time the system recorded for this signal (used for owner actions that are not caused by new market data)."""
    return db.scalar(select(IntradaySignalEvent.observed_data_time).where(IntradaySignalEvent.signal_id == signal_id,
                                                                          IntradaySignalEvent.observed_data_time.is_not(None))
                     .order_by(IntradaySignalEvent.event_time.desc()).limit(1))


def record_rejection(db: Session, *, user_id: UUID, scan_run_id: UUID, strategy_id: UUID, strategy_version_id: UUID, instrument_id: UUID, direction: str | None,
                     reason_code: str, recorded_at: datetime, observed_data_time: datetime | None, reason_detail: str | None = None,
                     payload: dict[str, Any] | None = None) -> ScanRejection:
    if reason_code not in REJECTION_REASON_CODES:
        raise ValueError(f"unknown rejection reason {reason_code!r}")
    row = ScanRejection(id=uuid4(), user_id=user_id, scan_run_id=scan_run_id, strategy_id=strategy_id, strategy_version_id=strategy_version_id, instrument_id=instrument_id,
                        direction=direction, reason_code=reason_code, reason_detail=(reason_detail or "")[:500] or None, recorded_at=_aware(recorded_at, "recorded_at"),
                        observed_data_time=_aware(observed_data_time, "observed_data_time") if observed_data_time else None, payload=payload or {})
    db.add(row)
    return row
