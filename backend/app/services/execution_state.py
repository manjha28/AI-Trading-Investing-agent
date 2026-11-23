"""ONE source of truth for "can this app send a real order right now" per venue (Dhan, Delta).

Every screen and API that talks about live execution reads this, never its own flag logic or a hard-coded sentence.
Effective state:
  OFF                the deployment flag is off, or the owner has not armed the venue
  ARMED_BUT_BLOCKED  flag on and armed, but something stops an order right now (the reasons are listed)
  LIVE               flag on, armed, trading window open, at least one strategy has its live switch on
A "blocked attempt" never reached the broker; the order ledger lists only orders that were sent (a broker rejection is a real order).
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

IST = ZoneInfo("Asia/Kolkata")
SENT_STATUSES_EXCLUDED = ("blocked",)      # everything else was sent to the broker (placed, filled, closed, cancelled, rejected by the broker, error after sending)


def _state(flag_on: bool, armed: bool, blockers: list[str], flag_name: str) -> dict[str, Any]:
    if not flag_on:
        return {"state": "OFF", "reasons": [f"the deployment flag {flag_name} is off"]}
    if not armed:
        return {"state": "OFF", "reasons": ["you have not armed live execution"]}
    if blockers:
        return {"state": "ARMED_BUT_BLOCKED", "reasons": blockers}
    return {"state": "LIVE", "reasons": []}


def _today(rows: list[Any]) -> dict[str, int]:
    sent = [r for r in rows if r.status not in SENT_STATUSES_EXCLUDED]
    return {"sent_to_broker": len(sent), "rejected_by_broker": sum(1 for r in sent if r.status in ("rejected", "error")), "blocked_attempts": sum(1 for r in rows if r.status == "blocked")}


def dhan_state(db: Session, user_id: UUID, now: datetime | None = None) -> dict[str, Any]:
    from app.dhan_execution import policy
    from app.models.forward_live import DhanLiveOrder, DhanLiveState, ForwardTest
    now = now or datetime.now(timezone.utc)
    state = db.get(DhanLiveState, user_id)
    armed = bool(state and state.armed)
    local = now.astimezone(IST)
    lo, hi = policy.ENTRY_WINDOW_IST
    open_now = local.weekday() < 5 and lo <= local.time() <= hi
    live_tests = db.scalars(select(ForwardTest).where(ForwardTest.user_id == user_id, ForwardTest.execute_live.is_(True), ForwardTest.status == "active")).all()
    blockers: list[str] = []
    if policy.environment_block():
        blockers.append(f"live orders are blocked in this environment ({policy.environment_block()})")
    if not open_now:
        blockers.append(f"outside the entry window ({lo:%H:%M}-{hi:%H:%M} IST, Mon-Fri); it is {local:%a %H:%M} IST")
    if not live_tests:
        blockers.append("no forward test has its live switch on")
    since = local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    today = db.scalars(select(DhanLiveOrder).where(DhanLiveOrder.user_id == user_id, DhanLiveOrder.created_at >= since)).all()
    return {
        "venue": "dhan", "label": "Dhan (NSE equity, intraday)", "deployment_flag": {"name": "DHAN_LIVE_EXECUTION_ENABLED", "enabled": policy.deployment_enabled()},
        "armed": armed, "armed_at": state.armed_at.isoformat() if state and state.armed_at else None,
        "window": {"kind": "session", "text": f"Mon-Fri {lo:%H:%M}-{hi:%H:%M} IST", "open_now": open_now},
        "strategies": [{"key": k, "live_tests": sum(1 for t in live_tests if t.strategy_key == k)} for k in sorted(policy.ALLOWED_STRATEGY_KEYS)],
        "live_tests": len(live_tests), "effective": _state(policy.deployment_enabled(), armed, blockers, "DHAN_LIVE_EXECUTION_ENABLED"),
        "limits": {"max_quantity": policy.MAX_ORDER_QUANTITY, "product": policy.PRODUCT_TYPE}, "today": _today(list(today)),
    }


def delta_state(db: Session, user_id: UUID, now: datetime | None = None) -> dict[str, Any]:
    from app.delta_execution import policy, service
    from app.models.crypto_delta import CryptoForwardTest, DeltaLiveOrder, DeltaLiveState
    now = now or datetime.now(timezone.utc)
    state = db.get(DeltaLiveState, user_id)
    armed = bool(state and state.armed)
    live_tests = db.scalars(select(CryptoForwardTest).where(CryptoForwardTest.user_id == user_id, CryptoForwardTest.execute_live.is_(True), CryptoForwardTest.status == "active")).all()
    blockers: list[str] = []
    if policy.environment_block():
        blockers.append(f"live orders are blocked in this environment ({policy.environment_block()})")
    if not policy.credentials_present():
        blockers.append("Delta API credentials are not configured on this deployment")
    if not live_tests:
        blockers.append("no Delta forward test has its live switch on")
    today = db.scalars(select(DeltaLiveOrder).where(DeltaLiveOrder.user_id == user_id, DeltaLiveOrder.created_at >= service.day_start_utc(now))).all()
    return {
        "venue": "delta", "label": "Delta Exchange India (USD perpetuals)", "deployment_flag": {"name": "DELTA_LIVE_EXECUTION_ENABLED", "enabled": policy.deployment_enabled()},
        "armed": armed, "armed_at": state.armed_at.isoformat() if state and state.armed_at else None,
        "window": {"kind": "24x7", "text": "24 hours, 7 days", "open_now": True},
        "strategies": [{"key": k, "live_tests": sum(1 for t in live_tests if (t.params or {}) is not None)} for k in sorted(policy.ALLOWED_STRATEGY_KEYS)],
        "live_tests": len(live_tests), "effective": _state(policy.deployment_enabled(), armed, blockers, "DELTA_LIVE_EXECUTION_ENABLED"),
        "limits": {"max_contracts": policy.MAX_CONTRACTS, "leverage": f"min({policy.LEVERAGE_CAP}x, the contract's exchange maximum)"}, "today": _today(list(today)),
    }


def all_states(db: Session, user_id: UUID, now: datetime | None = None) -> dict[str, Any]:
    return {"dhan": dhan_state(db, user_id, now), "delta": delta_state(db, user_id, now), "as_of": (now or datetime.now(timezone.utc)).isoformat()}


def describe(state: dict[str, Any]) -> str:
    """The one sentence every screen may print about a venue: its effective state and, when it is not LIVE, why."""
    eff = state["effective"]
    head = f"{state['label']}: {eff['state'].replace('_', ' ')}"
    return head if eff["state"] == "LIVE" else f"{head} ({'; '.join(eff['reasons'])})"
