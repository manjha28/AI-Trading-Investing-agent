"""VAR intraday futures research API. Research, backtest and signal-diagnostic endpoints ONLY: no order placement, no broker access.

Every route: authenticated + email-verified; mutations require CSRF; every query is owner-scoped. Results are hypothetical."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.auth import require_csrf
from app.api.strategies import require_verified_session
from app.db import get_db
from app.models import UserSession
from app.models.var_strategy import VarBacktestRun, VarConfigVersion
from app.services import job_queue
from app.services.var import service
from app.services.var.config import VarConfig

router = APIRouter(prefix="/var", tags=["var-intraday"])
DISCLAIMER = "Decision support, not investment advice. Hypothetical research output; never an order or a real position."


class BacktestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    underlyings: list[str] = Field(min_length=1, max_length=25)
    config: dict[str, Any] | None = None
    initial_capital: Decimal = Field(default=Decimal("2000000"), gt=0, max_digits=18, decimal_places=2)
    start: date | None = None
    end: date | None = None
    stock_universe_top_n: int | None = Field(default=None, ge=1, le=50)
    segment: str = Field(default="index_futures", pattern="^(index_futures|stock_futures|equity_cash)$")
    broker_profile: str = Field(default="dhan", max_length=32)
    exchange: str = Field(default="NSE", max_length=8)
    fee_schedule_version: str | None = Field(default=None, max_length=64)
    allow_partial: bool = False       # run only the symbols that have usable stored data; otherwise any unusable symbol rejects the request


def _latest_config(db: Session, uid: UUID) -> VarConfigVersion | None:
    return db.scalar(select(VarConfigVersion).where(VarConfigVersion.user_id == uid).order_by(VarConfigVersion.version_no.desc()).limit(1))


def _validate(cfg: dict) -> VarConfig:
    try:
        return VarConfig(**cfg)
    except ValidationError as error:
        raise HTTPException(status_code=422, detail=[{"loc": list(e["loc"]), "msg": e["msg"]} for e in error.errors()]) from None


@router.get("/config")
def get_config(active_session: UserSession = Depends(require_verified_session), db: Session = Depends(get_db)) -> dict:
    row = _latest_config(db, active_session.user_id)
    cfg = VarConfig(**row.config) if row else VarConfig()
    return {"version_no": row.version_no if row else 0, "saved": row is not None, "config": cfg.model_dump(mode="json"), "disclaimer": DISCLAIMER}


@router.put("/config")
def save_config(body: dict[str, Any], active_session: UserSession = Depends(require_verified_session), _: UserSession = Depends(require_csrf),
                db: Session = Depends(get_db)) -> dict:
    """Append a new config version. Older versions and existing backtest snapshots are never rewritten."""
    cfg = _validate(body)
    nxt = (db.scalar(select(func.max(VarConfigVersion.version_no)).where(VarConfigVersion.user_id == active_session.user_id)) or 0) + 1
    row = VarConfigVersion(user_id=active_session.user_id, version_no=nxt, config=cfg.model_dump(mode="json"), created_at=datetime.now(timezone.utc))
    db.add(row)
    db.commit()
    return {"version_no": nxt, "config": row.config}


@router.get("/data-capabilities")
def data_capabilities(active_session: UserSession = Depends(require_verified_session), db: Session = Depends(get_db)) -> dict:
    return {**service.data_capabilities(db), "disclaimer": DISCLAIMER}


@router.post("/backtests", status_code=202)
def create_backtest(body: BacktestIn, active_session: UserSession = Depends(require_verified_session), _: UserSession = Depends(require_csrf),
                    db: Session = Depends(get_db)) -> dict:
    row = _latest_config(db, active_session.user_id)
    base = dict(body.config if body.config is not None else (row.config if row else {}))
    base["charges"] = {"broker_profile": body.broker_profile, "exchange": body.exchange, "fee_schedule_version": body.fee_schedule_version}
    cfg = _validate(base)
    if cfg.uses_orderflow and not service.ORDER_FLOW_STORED:
        raise HTTPException(status_code=422, detail={"code": "order_flow_unavailable", "message": f"Variant {cfg.variant} needs trade-level order flow (delta/CVD), which is not stored, so it cannot produce a trade. Choose variant A."})
    pre = service.preflight(db, body.segment, body.underlyings, body.start, body.end)
    usable = [p["symbol"] for p in pre if p["status"] == "ok"]
    if not usable:
        raise HTTPException(status_code=422, detail={"code": "no_usable_data", "message": "None of the requested symbols has usable stored data for this segment and window; nothing was queued.", "symbols": pre})
    if len(usable) < len(pre) and not body.allow_partial:
        raise HTTPException(status_code=422, detail={"code": "unusable_symbols", "message": "Some requested symbols have no usable stored data. Remove them, or resubmit with allow_partial to run only the usable ones.",
                                                     "symbols": pre, "usable": usable})
    dropped = [p for p in pre if p["status"] != "ok"]
    body = body.model_copy(update={"underlyings": usable})
    run = VarBacktestRun(user_id=active_session.user_id, status="queued", hypothetical=True, config_snapshot=cfg.model_dump(mode="json"),
                         request=service.jsonable({**body.model_dump(mode="json"), "preflight": pre, "dropped_symbols": dropped}), created_at=datetime.now(timezone.utc))
    db.add(run)
    db.commit()
    job_queue.enqueue(db, user_id=active_session.user_id, job_type="var_backtest", payload={"run_id": str(run.id)}, max_attempts=1,
                      idempotency_key=f"var_backtest:{run.id}")
    return {"id": str(run.id), "status": "queued", "hypothetical": True, "disclaimer": DISCLAIMER}


def _out(r: VarBacktestRun, full: bool) -> dict:
    d = {"id": str(r.id), "status": r.status, "hypothetical": True, "created_at": r.created_at.isoformat(), "finished_at": r.finished_at.isoformat() if r.finished_at else None,
         "request": r.request, "config_snapshot": r.config_snapshot, "error": r.error, "disclaimer": DISCLAIMER}
    d["result"] = r.result if full else ({"summary": r.result.get("summary"), "window": r.result.get("window")} if r.result else None)
    return d


@router.get("/backtests")
def list_backtests(active_session: UserSession = Depends(require_verified_session), db: Session = Depends(get_db)) -> list[dict]:
    rows = db.scalars(select(VarBacktestRun).where(VarBacktestRun.user_id == active_session.user_id).order_by(VarBacktestRun.created_at.desc()).limit(50))
    return [_out(r, False) for r in rows]


@router.get("/backtests/{run_id}")
def get_backtest(run_id: UUID, active_session: UserSession = Depends(require_verified_session), db: Session = Depends(get_db)) -> dict:
    r = db.scalar(select(VarBacktestRun).where(VarBacktestRun.id == run_id, VarBacktestRun.user_id == active_session.user_id))
    if r is None:
        raise HTTPException(status_code=404, detail="Backtest run not found")
    return _out(r, True)


@router.get("/charges/schedule")
def charges_schedule(segment: str = "futures", instrument_type: str = "index_future", on: date | None = None, broker_profile: str = "dhan", exchange: str = "NSE",
                     active_session: UserSession = Depends(require_verified_session)) -> dict:
    """The full applicable Indian charge schedule (every component with rate, effective date, verification status). Never a partial or zero schedule."""
    from app.services import indian_charges as ic
    try:
        return {**ic.schedule_report(segment, instrument_type, on or date.today(), broker_profile, exchange), "disclaimer": DISCLAIMER}
    except ic.SegmentMismatch as error:
        raise HTTPException(status_code=422, detail=str(error)) from None
    except ic.ChargeError as error:
        raise HTTPException(status_code=409, detail={"code": "schedule_unavailable", "message": str(error)}) from None


@router.get("/preflight")
def preflight(symbols: str, segment: str = "index_futures", start: date | None = None, end: date | None = None,
              active_session: UserSession = Depends(require_verified_session), db: Session = Depends(get_db)) -> dict:
    """Check requested symbols against STORED data before launching: not in storage / no bars / no usable sessions / ok."""
    if segment not in service.SEGMENTS:
        raise HTTPException(status_code=422, detail="unknown segment")
    names = [x.strip().upper() for x in symbols.split(",") if x.strip()][:40]
    if not names:
        raise HTTPException(status_code=422, detail="no symbols given")
    rows = service.preflight(db, segment, names, start, end)
    return {"segment": segment, "symbols": rows, "usable": [r["symbol"] for r in rows if r["status"] == "ok"], "order_flow_stored": service.ORDER_FLOW_STORED, "disclaimer": DISCLAIMER}
