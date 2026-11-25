"""Delta live-execution controls. Owner-approved exception (AGENTS.md): off by default in three layers, 1 contract max, leverage = min(25x, exchange max)."""
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.auth import get_active_session, require_csrf
from app.api.crypto_delta import require_verified_session
from app.db import get_db
from app.delta_execution import policy, service as live
from app.models import UserSession
from app.services import execution_state
from app.models.crypto_delta import CryptoForwardTest, DeltaLiveOrder

router = APIRouter(prefix="/crypto-delta/live", tags=["crypto-delta-live"])
DISCLAIMER = "Decision support, not investment advice. Real orders on your own account."


def require_arming_session(active_session: UserSession = Depends(require_verified_session)) -> UserSession:
    if not active_session.user.two_factor_enabled or not active_session.two_factor_verified:
        raise HTTPException(status_code=403, detail={"code": "two_factor_required", "message": "Arming live execution needs a two-factor-verified session."})
    return active_session


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _status(db: Session, user_id: UUID) -> dict[str, Any]:
    state = live.get_state(db, user_id)
    today = db.scalars(select(DeltaLiveOrder).where(DeltaLiveOrder.user_id == user_id, DeltaLiveOrder.created_at >= live.day_start_utc(_now())).order_by(DeltaLiveOrder.created_at.desc())).all()
    db.commit()
    return {
        "deployment_enabled": policy.deployment_enabled(), "credentials_present": policy.credentials_present(), "environment_block": policy.environment_block(), "armed": state.armed,
        "armed_at": state.armed_at.isoformat() if state.armed_at else None,
        "can_arm": policy.deployment_enabled() and policy.credentials_present() and policy.environment_block() is None,
        "limits": {"max_contracts": policy.MAX_CONTRACTS, "leverage": f"min({policy.LEVERAGE_CAP}x, the contract's exchange maximum)", "allowed_strategies": sorted(policy.ALLOWED_STRATEGY_KEYS),
                   "max_daily_loss_usd": str(state.max_daily_loss_usd), "max_trades_per_day": state.max_trades_per_day, "max_open_positions": state.max_open_positions,
                   "max_notional_usd": str(state.max_notional_usd)},
        "today": {"orders": len(today), "placed": sum(r.status in live.OPEN or r.status == "closed" for r in today), "blocked": sum(r.status == "blocked" for r in today),
                  "rejected": sum(r.status in ("rejected", "error") for r in today)},
        "confirmation_phrase": policy.CONFIRMATION_PHRASE,
        "execution": execution_state.delta_state(db, user_id),
        "warning": "Real orders on your own account: one contract at most, leveraged. The 5 EMA strategy has no proven edge; expect losses as well as wins.",
        "disclaimer": DISCLAIMER,
    }


@router.get("/status")
def live_status(active_session: UserSession = Depends(require_verified_session), db: Session = Depends(get_db)) -> dict[str, Any]:
    return _status(db, active_session.user_id)


class ArmBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    acknowledged: bool = False   # set by the warning dialog the owner confirms; no phrase is typed any more
    max_daily_loss_usd: Decimal = Field(default=Decimal("25"), ge=1, le=500)
    max_trades_per_day: int = Field(default=6, ge=1, le=20)
    max_open_positions: int = Field(default=2, ge=1, le=5)
    max_notional_usd: Decimal = Field(default=Decimal("2000"), ge=10, le=10000)


@router.post("/arm")
def arm(body: ArmBody, active_session: UserSession = Depends(require_arming_session), _: UserSession = Depends(require_csrf), db: Session = Depends(get_db)) -> dict[str, Any]:
    """The explicit, auditable owner action that allows live orders. Needs the deployment flag, credentials, a 2FA-verified session and the typed phrase."""
    if not body.acknowledged:
        raise HTTPException(status_code=422, detail="Confirm the warning to switch live orders on.")
    if not policy.deployment_enabled():
        raise HTTPException(status_code=409, detail="Delta live execution is switched off on this deployment (DELTA_LIVE_EXECUTION_ENABLED is not set).")
    if not policy.credentials_present():
        raise HTTPException(status_code=409, detail="Delta API credentials are not configured on this deployment.")
    blocked = policy.environment_block()
    if blocked:
        raise HTTPException(status_code=409, detail=f"Live execution is unavailable here ({blocked}).")
    now = _now()
    state = live.get_state(db, active_session.user_id)
    state.armed, state.armed_at, state.updated_at = True, now, now
    state.max_daily_loss_usd, state.max_trades_per_day = body.max_daily_loss_usd, body.max_trades_per_day
    state.max_open_positions, state.max_notional_usd = body.max_open_positions, body.max_notional_usd
    live.audit(db, active_session.user_id, "decision", uuid4(), "arm", "-", {"event": "Delta live execution ARMED by owner", "limits": {
        "max_daily_loss_usd": str(body.max_daily_loss_usd), "max_trades_per_day": body.max_trades_per_day, "max_open_positions": body.max_open_positions,
        "max_notional_usd": str(body.max_notional_usd)}}, now)
    db.commit()
    return _status(db, active_session.user_id)


@router.post("/disarm")
def disarm(active_session: UserSession = Depends(require_verified_session), _: UserSession = Depends(require_csrf), db: Session = Depends(get_db)) -> dict[str, Any]:
    """KILL SWITCH: disarm immediately, then cancel this app's unfilled orders. Never needs 2FA: stopping must always be easy."""
    result = live.kill_switch(db, active_session.user_id)
    return {**result, "status": _status(db, active_session.user_id)}


class LiveFlag(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


@router.put("/tests/{ft_id}")
def set_execute_live(ft_id: UUID, body: LiveFlag, active_session: UserSession = Depends(require_verified_session), _: UserSession = Depends(require_csrf),
                     db: Session = Depends(get_db)) -> dict[str, Any]:
    ft = db.scalar(select(CryptoForwardTest).where(CryptoForwardTest.id == ft_id, CryptoForwardTest.user_id == active_session.user_id))
    if ft is None:
        raise HTTPException(status_code=404, detail="Forward test not found.")
    if body.enabled:
        if ft.contracts > policy.MAX_CONTRACTS:
            raise HTTPException(status_code=422, detail=f"Live size is capped at {policy.MAX_CONTRACTS} contract.")
        if ft.resolution == "1h" or (ft.params or {}).get("ema") != 5:
            raise HTTPException(status_code=422, detail="Only the 5 EMA alert-candle test on 5m or 15m candles can execute live.")
    ft.execute_live, ft.updated_at = body.enabled, _now()
    live.audit(db, active_session.user_id, "decision", uuid4(), "execute-live", ft.symbol, {"event": "per-test execute_live", "enabled": body.enabled, "test": str(ft.id)}, _now())
    db.commit()
    return {"id": str(ft.id), "execute_live": ft.execute_live}


@router.post("/reconcile")
def reconcile_now(active_session: UserSession = Depends(require_verified_session), _: UserSession = Depends(require_csrf), db: Session = Depends(get_db)) -> dict[str, int]:
    return live.reconcile(db, active_session.user_id)


@router.get("/orders")
def live_orders(limit: int = Query(default=50, ge=1, le=200), active_session: UserSession = Depends(require_verified_session), db: Session = Depends(get_db)) -> dict[str, Any]:
    all_rows = db.scalars(select(DeltaLiveOrder).where(DeltaLiveOrder.user_id == active_session.user_id).order_by(DeltaLiveOrder.created_at.desc()).limit(limit)).all()
    rows = [r for r in all_rows if r.status != "blocked"]                 # the ledger lists orders that were SENT to Delta (a broker rejection is a real order)
    blocked = [r for r in all_rows if r.status == "blocked"]
    return {"blocked_attempts": [{"id": str(r.id), "created_at": r.created_at.isoformat(), "symbol": r.symbol, "side": r.side, "contracts": r.contracts, "block_reason": r.block_reason} for r in blocked], "orders": [{"id": str(r.id), "created_at": r.created_at.isoformat(), "symbol": r.symbol, "side": r.side, "contracts": r.contracts, "leverage": r.leverage,
                        "status": r.status, "block_reason": r.block_reason, "reference_price": str(r.reference_price), "stop_price": str(r.stop_price) if r.stop_price else None,
                        "target_price": str(r.target_price) if r.target_price else None, "entry_price": str(r.entry_price) if r.entry_price else None,
                        "delta_order_id": r.delta_order_id, "broker_response": r.response} for r in rows],
            "note": "Outcomes and P&L are on Delta; this list records what the app sent and what Delta answered.", "disclaimer": DISCLAIMER}


@router.get("/report")
def live_report(force: bool = Query(default=False), active_session: UserSession = Depends(require_verified_session), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Live wallet, positions, open orders, realised P&L and the outcome of every live order, read from Delta. Stale (with the reason) if Delta cannot be read. Read only."""
    from app.delta_execution import report as live_report_module
    data = live_report_module.report(db, active_session.user_id, force=force)
    return {**{k: v for k, v in data.items() if k != "user_id"}, "disclaimer": DISCLAIMER}
