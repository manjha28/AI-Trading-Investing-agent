"""Forward tests (paper by default) and the guarded Dhan live-execution controls. Every route: authenticated + email-verified, CSRF on mutations, owner-scoped."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.auth import get_active_session, require_csrf
from app.db import get_db
from app.dhan_execution import policy
from app.dhan_execution import service as live
from app.models import Instrument, Strategy, StrategyVersion, UserSession
from app.models.forward_live import DhanLiveOrder, ForwardTest
from app.services import execution_state
from app.services import forward_test as fts
from app.services import lab
from app.services.intraday_strategy import definition_hash, validate_definition, StrategyDefinition
from app.strategies import get as registry_get

router = APIRouter(prefix="/forward-tests", tags=["forward-tests"])
live_router = APIRouter(prefix="/dhan-live", tags=["dhan-live"])
DISCLAIMER = "Decision support, not investment advice."


def require_verified_session(active_session: UserSession = Depends(get_active_session)) -> UserSession:
    if not active_session.user.email_verified:
        raise HTTPException(status_code=403, detail={"code": "email_verification_required", "message": "Verify your email address first."})
    return active_session


def require_arming_session(active_session: UserSession = Depends(require_verified_session)) -> UserSession:
    if not active_session.user.two_factor_enabled or not active_session.two_factor_verified:
        raise HTTPException(status_code=403, detail={"code": "two_factor_required", "message": "Arming live execution needs a two-factor-verified session."})
    return active_session


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ───────────────────────── forward tests ─────────────────────────
OWN_PREFIX = "own:"


def _own_strategy(db: Session, user_id: UUID, key: str) -> tuple[str, dict[str, Any]]:
    """(name, definition) of one of this user's saved intraday strategies. Python/Pine-file and options strategies are not replayed here."""
    try:
        strategy_id = UUID(key.removeprefix(OWN_PREFIX))
    except ValueError:
        raise HTTPException(status_code=404, detail="Unknown strategy.") from None
    strategy = db.scalar(select(Strategy).where(Strategy.id == strategy_id, Strategy.user_id == user_id))
    version = None if strategy is None else db.scalar(select(StrategyVersion).where(StrategyVersion.strategy_id == strategy.id, StrategyVersion.version == strategy.current_version))
    if version is None or version.definition is None:
        raise HTTPException(status_code=404, detail="Unknown strategy.")
    raw = dict(version.definition)
    if raw.get("python") is not None or raw.get("options") is not None or raw.get("engine") is not None:
        raise HTTPException(status_code=422, detail="Only strategies built from conditions can be forward tested here (not Python, options or engine strategies).")
    return strategy.name, raw
class CreateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(default="", max_length=80)
    strategy_key: str = Field(min_length=1, max_length=64)
    params: dict[str, float | int] = Field(default_factory=dict)
    universe: Literal["master", "watchlist", "nifty50", "has_1m"] | None = None
    instruments: list[UUID] = Field(default_factory=list, max_length=lab.MAX_LAB_INSTRUMENTS)
    quantity: int = Field(default=1, ge=1, le=100)
    start_date: date | None = None
    slippage_bps: Decimal = Field(default=Decimal("2"), ge=0, le=100)
    cost_profile: str | None = Field(default=None, max_length=64)
    execute_live: bool = False

    @field_validator("slippage_bps", mode="before")
    @classmethod
    def no_floats(cls, value: Any) -> Any:
        if isinstance(value, float):
            raise ValueError("send numbers as strings or integers, never floats")
        return value


def _owned(db: Session, user_id: UUID, ft_id: UUID) -> ForwardTest:
    ft = db.scalar(select(ForwardTest).where(ForwardTest.id == ft_id, ForwardTest.user_id == user_id))
    if ft is None:
        raise HTTPException(status_code=404, detail="Forward test not found.")
    return ft


def _view(ft: ForwardTest, *, full: bool) -> dict[str, Any]:
    snap = ft.snapshot or {}
    base = {"id": str(ft.id), "name": ft.name, "strategy_key": ft.strategy_key, "status": ft.status, "quantity": ft.quantity, "start_date": ft.start_date.isoformat(),
            "execute_live": ft.execute_live, "instrument_count": len(ft.instrument_ids), "whole_market_watch_list": lab.MASTER_TOKEN in ft.instrument_ids, "created_at": ft.created_at.isoformat(),
            "last_run_at": ft.last_run_at.isoformat() if ft.last_run_at else None, "last_error": ft.last_error, "slippage_bps": str(ft.slippage_bps),
            "summary": snap.get("summary"), "waiting": snap.get("waiting"), "paper_only": not ft.execute_live, "label": fts.LABEL}
    if full:
        base |= {"snapshot": snap, "definition_hash": ft.definition_hash, "timeframe": ft.definition.get("decision_timeframe"), "instrument_ids": ft.instrument_ids}
    return base


@router.get("")
def list_forward_tests(active_session: UserSession = Depends(require_verified_session), db: Session = Depends(get_db)) -> dict[str, Any]:
    rows = db.scalars(select(ForwardTest).where(ForwardTest.user_id == active_session.user_id).order_by(ForwardTest.created_at.desc())).all()
    return {"forward_tests": [_view(r, full=False) for r in rows], "default_start_date": _default_start(_now()).isoformat(), "disclaimer": DISCLAIMER}


def _default_start(now: datetime) -> date:
    local = now.astimezone(fts.IST)
    if local.weekday() < 5 and local.time() < fts.time(15, 30):
        return local.date()
    return fts.next_trading_day(local.date())


@router.post("", status_code=201)
def create_forward_test(body: CreateBody, active_session: UserSession = Depends(require_verified_session), _: UserSession = Depends(require_csrf),
                        db: Session = Depends(get_db)) -> dict[str, Any]:
    own_universe: list[str] = []
    if body.strategy_key.startswith(OWN_PREFIX):                          # the owner's own saved strategy: paper only, never on the live allowlist
        if body.execute_live:
            raise HTTPException(status_code=422, detail="Your own strategies can only be forward tested on paper.")
        spec_name, raw = _own_strategy(db, active_session.user_id, body.strategy_key)
        own_universe = list(raw.get("universe") or [])
    else:
        try:
            spec = registry_get(body.strategy_key)
            raw = spec.definition(body.params)
        except KeyError:
            raise HTTPException(status_code=404, detail="Unknown registry strategy.") from None
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
        spec_name = spec.name
    defn = StrategyDefinition.model_validate(raw)
    errors = [i["message"] for i in validate_definition(defn)[0] if i["level"] == "error"]
    if errors:
        raise HTTPException(status_code=422, detail="; ".join(errors))
    if body.execute_live:
        if body.strategy_key not in policy.ALLOWED_STRATEGY_KEYS:
            raise HTTPException(status_code=422, detail=f"{body.strategy_key} is not on the live-execution allowlist ({', '.join(sorted(policy.ALLOWED_STRATEGY_KEYS))}).")
        if body.quantity > policy.MAX_ORDER_QUANTITY:
            raise HTTPException(status_code=422, detail=f"Live quantity is capped at {policy.MAX_ORDER_QUANTITY}.")
        if defn.stop.mode == "none" or defn.target.mode == "none":
            raise HTTPException(status_code=422, detail="Live orders need a strategy with both a stop and a target.")
    picked: dict[UUID, Instrument] = {}
    excluded: list[dict[str, str]] = []
    whole_list = body.universe == "master"
    if body.universe:
        u = lab.resolve_universe(db, active_session.user_id, body.universe)
        picked.update({i.id: i for i in u["instruments"]})
        excluded = u["excluded"]
    wanted = list(body.instruments) or ([UUID(x) for x in own_universe if x != lab.MASTER_TOKEN] if not body.universe else [])
    whole_list = whole_list or (not body.universe and not body.instruments and lab.MASTER_TOKEN in own_universe)
    if whole_list and not body.universe:
        picked.update({i.id: i for i in lab.resolve_universe(db, active_session.user_id, "master")["instruments"]})
    for iid in wanted:
        inst = db.get(Instrument, iid)
        if inst is None:
            raise HTTPException(status_code=404, detail=f"instrument {iid} not found")
        picked[inst.id] = inst
    if not picked:
        raise HTTPException(status_code=422, detail={"code": "no_instruments", "message": "Choose a symbol set or instruments with stored 1-minute data.", "excluded": excluded})
    if len(picked) > (lab.MAX_MASTER_INSTRUMENTS if whole_list else lab.MAX_LAB_INSTRUMENTS):
        raise HTTPException(status_code=422, detail=f"At most {lab.MAX_LAB_INSTRUMENTS} instruments per forward test (the whole Market watch list is allowed).")
    if body.execute_live and not whole_list:
        no_id = [i.symbol for i in picked.values() if fts._security_id(db, i) is None]
        if no_id:
            raise HTTPException(status_code=422, detail=f"No verified Dhan security id for: {', '.join(no_id)}; live orders need one.")
    now = _now()
    suffix = f" ({int(body.params['timeframe_min'])}m)" if "timeframe_min" in body.params else ""
    ft = ForwardTest(user_id=active_session.user_id, name=(body.name.strip() or f"{spec_name}{suffix}")[:80], strategy_key=body.strategy_key, definition=raw,
                     definition_hash=definition_hash(raw), instrument_ids=([lab.MASTER_TOKEN] if whole_list else [str(i) for i in picked]), quantity=body.quantity,
                     start_date=body.start_date or _default_start(now), status="active", execute_live=body.execute_live, slippage_bps=body.slippage_bps,
                     cost_profile=body.cost_profile, created_at=now, updated_at=now)
    db.add(ft)
    db.commit()
    db.refresh(ft)
    return {**_view(ft, full=False), "excluded": excluded, "whole_market_watch_list": whole_list,
            "instruments": [{"id": str(i.id), "symbol": i.symbol} for i in list(picked.values())[:60]], "instrument_total": len(picked)}


@router.get("/{ft_id}")
def get_forward_test(ft_id: UUID, active_session: UserSession = Depends(require_verified_session), db: Session = Depends(get_db)) -> dict[str, Any]:
    return _view(_owned(db, active_session.user_id, ft_id), full=True)


@router.post("/{ft_id}/refresh")
def refresh_forward_test(ft_id: UUID, active_session: UserSession = Depends(require_verified_session), _: UserSession = Depends(require_csrf),
                         db: Session = Depends(get_db)) -> dict[str, Any]:
    """Run the test now (data refresh + replay). A live test still goes through every guard-rail; this never bypasses them."""
    ft = _owned(db, active_session.user_id, ft_id)
    fts.run_once(db, ft, _now(), client_factory=(lambda uid: fts.build_client(db, uid)) if ft.execute_live else None)
    db.refresh(ft)
    return _view(ft, full=True)


@router.post("/{ft_id}/{action}")
def set_status(ft_id: UUID, action: Literal["pause", "resume"], active_session: UserSession = Depends(require_verified_session),
               _: UserSession = Depends(require_csrf), db: Session = Depends(get_db)) -> dict[str, Any]:
    ft = _owned(db, active_session.user_id, ft_id)
    ft.status, ft.updated_at = ("paused" if action == "pause" else "active"), _now()
    db.commit()
    return _view(ft, full=False)


class LiveFlag(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


@router.put("/{ft_id}/execute-live")
def set_execute_live(ft_id: UUID, body: LiveFlag, active_session: UserSession = Depends(require_verified_session), _: UserSession = Depends(require_csrf),
                     db: Session = Depends(get_db)) -> dict[str, Any]:
    ft = _owned(db, active_session.user_id, ft_id)
    if body.enabled:
        if ft.strategy_key not in policy.ALLOWED_STRATEGY_KEYS:
            raise HTTPException(status_code=422, detail="This strategy is not on the live-execution allowlist.")
        if ft.quantity > policy.MAX_ORDER_QUANTITY:
            raise HTTPException(status_code=422, detail=f"Live quantity is capped at {policy.MAX_ORDER_QUANTITY}.")
    ft.execute_live, ft.updated_at = body.enabled, _now()
    db.commit()
    return _view(ft, full=False)


@router.delete("/{ft_id}", status_code=204)
def delete_forward_test(ft_id: UUID, active_session: UserSession = Depends(require_verified_session), _: UserSession = Depends(require_csrf),
                        db: Session = Depends(get_db)) -> Response:
    ft = _owned(db, active_session.user_id, ft_id)
    db.delete(ft)
    db.commit()
    return Response(status_code=204)


# ───────────────────────── Dhan live execution controls ─────────────────────────
def _status(db: Session, user_id: UUID) -> dict[str, Any]:
    state = live.get_state(db, user_id)
    now = _now()
    since = live._day_start_utc(now)
    today = db.scalars(select(DhanLiveOrder).where(DhanLiveOrder.user_id == user_id, DhanLiveOrder.created_at >= since).order_by(DhanLiveOrder.created_at.desc())).all()
    db.commit()
    return {
        "deployment_enabled": policy.deployment_enabled(), "environment_block": policy.environment_block(), "armed": state.armed,
        "armed_at": state.armed_at.isoformat() if state.armed_at else None,
        "can_arm": policy.deployment_enabled() and policy.environment_block() is None,
        "limits": {"max_order_quantity": policy.MAX_ORDER_QUANTITY, "allowed_strategies": sorted(policy.ALLOWED_STRATEGY_KEYS), "product": policy.PRODUCT_TYPE, "segment": policy.EXCHANGE_SEGMENT,
                   "entry_window_ist": [policy.ENTRY_WINDOW_IST[0].strftime("%H:%M"), policy.ENTRY_WINDOW_IST[1].strftime("%H:%M")],
                   "max_daily_loss_inr": str(state.max_daily_loss_inr), "max_trades_per_day": state.max_trades_per_day,
                   "max_open_positions": state.max_open_positions, "max_notional_inr": str(state.max_notional_inr)},
        "today": {"orders": len(today), "placed": sum(r.status in live.OPEN or r.status == "closed" for r in today), "blocked": sum(r.status == "blocked" for r in today),
                  "rejected": sum(r.status in ("rejected", "error") for r in today),
                  "realized_pnl": str(sum((r.realized_pnl for r in today if r.realized_pnl is not None), Decimal(0)))},
        "confirmation_phrase": policy.CONFIRMATION_PHRASE,
        "execution": execution_state.dhan_state(db, user_id, now),
        "warning": "Real orders on your own account. Quantity is capped at 1. Backtests of these strategies were net-negative after charges; expect small losses, not profit.",
        "disclaimer": DISCLAIMER,
    }


@live_router.get("/status")
def live_status(active_session: UserSession = Depends(require_verified_session), db: Session = Depends(get_db)) -> dict[str, Any]:
    return _status(db, active_session.user_id)


class ArmBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    acknowledged: bool = False   # set by the warning dialog the owner confirms; no phrase is typed any more
    max_daily_loss_inr: Decimal = Field(default=Decimal("500"), ge=50, le=5000)
    max_trades_per_day: int = Field(default=10, ge=1, le=20)
    max_open_positions: int = Field(default=3, ge=1, le=5)
    max_notional_inr: Decimal = Field(default=Decimal("50000"), ge=1000, le=100000)


@live_router.post("/arm")
def arm(body: ArmBody, active_session: UserSession = Depends(require_arming_session), _: UserSession = Depends(require_csrf), db: Session = Depends(get_db)) -> dict[str, Any]:
    """The explicit, auditable owner action that allows live orders. Needs the deployment flag, a 2FA-verified session and the typed phrase."""
    if not body.acknowledged:
        raise HTTPException(status_code=422, detail="Confirm the warning to switch live orders on.")
    if not policy.deployment_enabled():
        raise HTTPException(status_code=409, detail="Live execution is switched off on this deployment (DHAN_LIVE_EXECUTION_ENABLED is not set).")
    blocked = policy.environment_block()
    if blocked:
        raise HTTPException(status_code=409, detail=f"Live execution is unavailable here ({blocked}).")
    now = _now()
    state = live.get_state(db, active_session.user_id)
    state.armed, state.armed_at, state.updated_at = True, now, now
    state.max_daily_loss_inr, state.max_trades_per_day = body.max_daily_loss_inr, body.max_trades_per_day
    state.max_open_positions, state.max_notional_inr = body.max_open_positions, body.max_notional_inr
    from uuid import uuid4
    live.audit(db, active_session.user_id, "decision", uuid4(), "arm", "-", {"event": "live execution ARMED by owner", "limits": {"max_daily_loss_inr": str(body.max_daily_loss_inr),
               "max_trades_per_day": body.max_trades_per_day, "max_open_positions": body.max_open_positions, "max_notional_inr": str(body.max_notional_inr)}}, now)
    db.commit()
    return _status(db, active_session.user_id)


@live_router.post("/disarm")
def disarm(active_session: UserSession = Depends(require_verified_session), _: UserSession = Depends(require_csrf), db: Session = Depends(get_db)) -> dict[str, Any]:
    """KILL SWITCH: disarm immediately, then cancel this app's unfilled orders. Never needs 2FA: stopping must always be easy."""
    result = live.kill_switch(db, active_session.user_id, lambda uid: fts.build_client(db, uid))
    return {**result, "status": _status(db, active_session.user_id)}


@live_router.post("/reconcile")
def reconcile_now(active_session: UserSession = Depends(require_verified_session), _: UserSession = Depends(require_csrf), db: Session = Depends(get_db)) -> dict[str, Any]:
    return live.reconcile(db, active_session.user_id, lambda uid: fts.build_client(db, uid))


@live_router.get("/orders")
def live_orders(limit: int = Query(default=50, ge=1, le=200), active_session: UserSession = Depends(require_verified_session), db: Session = Depends(get_db)) -> dict[str, Any]:
    all_rows = db.scalars(select(DhanLiveOrder).where(DhanLiveOrder.user_id == active_session.user_id).order_by(DhanLiveOrder.created_at.desc()).limit(limit)).all()
    rows = [r for r in all_rows if r.status != "blocked"]               # the ledger lists orders that were SENT to Dhan (a broker rejection is a real order)
    blocked = [r for r in all_rows if r.status == "blocked"]
    return {"orders": [{"id": str(r.id), "created_at": r.created_at.isoformat(), "symbol": r.symbol, "side": r.side, "quantity": r.quantity, "strategy_key": r.strategy_key,
                        "status": r.status, "block_reason": r.block_reason, "reference_price": str(r.reference_price), "stop_price": str(r.stop_price) if r.stop_price else None,
                        "target_price": str(r.target_price) if r.target_price else None, "entry_price": str(r.entry_price) if r.entry_price else None,
                        "exit_price": str(r.exit_price) if r.exit_price else None, "exit_reason": r.exit_reason,
                        "realized_pnl_estimate": str(r.realized_pnl) if r.realized_pnl is not None else None, "dhan_order_id": r.dhan_order_id,
                        "broker_response": r.response} for r in rows],
            "blocked_attempts": [{"id": str(r.id), "created_at": r.created_at.isoformat(), "symbol": r.symbol, "side": r.side, "quantity": r.quantity, "strategy_key": r.strategy_key, "block_reason": r.block_reason} for r in blocked],
            "note": "realized_pnl_estimate is gross P&L less this app's ESTIMATE of statutory charges; Dhan's contract note is the authority.", "disclaimer": DISCLAIMER}
