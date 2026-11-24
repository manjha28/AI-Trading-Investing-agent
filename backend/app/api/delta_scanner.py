"""Delta multi-perpetual scanner (paper only). Authenticated, email-verified, CSRF on mutations, every query owner-scoped. Nothing here can place an order."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.auth import require_csrf
from app.api.crypto_delta import require_verified_session
from app.crypto_delta.candles import CandleDataError
from app.crypto_delta.forward import RESOLUTIONS, fetch_spec
from app.db import get_db
from app.delta_scanner import service, universe
from app.delta_scanner.portfolio import LABEL
from app.models import UserSession
from app.models.delta_scanner import DeltaScanner

router = APIRouter(prefix="/delta-scanner", tags=["delta-scanner"])
MAX_SYMBOLS = 25


class ScannerCreate(BaseModel):
    name: str | None = Field(default=None, max_length=80)
    symbols: str = Field(default="", max_length=800)                     # comma / space separated; empty with top_liquid true
    top_liquid: int = Field(default=0, ge=0, le=MAX_SYMBOLS)             # "Top N most liquid": 24 h turnover and a tight spread, from Delta's tickers
    resolution: str = "15m"
    strategy_key: str = "five_ema_alert"
    wallet_usd: Decimal = Field(default=Decimal(1000), gt=0, le=Decimal(10_000_000))
    risk_pct: Decimal = Field(default=Decimal("0.005"), gt=0, le=Decimal("0.01"))
    total_risk_cap_pct: Decimal = Field(default=Decimal("0.02"), gt=0, le=Decimal("0.05"))
    leverage_cap: Decimal = Field(default=Decimal(5), gt=0, le=Decimal(5))   # the owner's effective cap; cannot be raised here
    ema: int = Field(default=5, ge=2, le=50)
    rr: Decimal = Field(default=Decimal(3), gt=0, le=Decimal(10))
    direction: int = Field(default=-1, ge=-1, le=1)
    slippage_bps: Decimal = Field(default=Decimal(5), ge=0, le=Decimal(100))


class Batch(BaseModel):
    symbols: str = Field(max_length=800)


def _json(sc: DeltaScanner, *, full: bool) -> dict[str, Any]:
    snap = sc.snapshot or {}
    out = {"id": str(sc.id), "name": sc.name, "strategy_key": sc.strategy_key, "resolution": sc.resolution, "symbols": sc.symbols, "status": sc.status, "label": sc.label,
           "paper_only": True, "wallet_usd": str(sc.wallet_usd), "risk_pct": str(sc.risk_pct), "total_risk_cap_pct": str(sc.total_risk_cap_pct), "leverage_cap": str(sc.leverage_cap),
           "start_time": sc.start_time.isoformat(), "last_run_at": sc.last_run_at.isoformat() if sc.last_run_at else None, "last_error": sc.last_error,
           "total_net_pnl": snap.get("total_net_pnl"), "per_symbol": snap.get("per_symbol"), "signals_seen": snap.get("signals_seen"), "signals_taken": snap.get("signals_taken"), "label_text": LABEL}
    if full:
        out.update(trades=(snap.get("trades") or [])[-100:], signal_log=snap.get("signal_log"), errors=snap.get("errors"), open_signals=snap.get("open_signals"), live_note=snap.get("live_note"))
    return out


def _owned(db: Session, user_id: Any, sid: UUID) -> DeltaScanner:
    row = db.scalar(select(DeltaScanner).where(DeltaScanner.id == sid, DeltaScanner.user_id == user_id))
    if row is None:
        raise HTTPException(status_code=404, detail="Scanner not found.")
    return row


@router.get("/universe/top")
def top_universe(n: int = 10, _: UserSession = Depends(require_verified_session)) -> dict[str, Any]:
    """The most liquid USD perpetuals right now: turnover, spread and funding for each (refreshed on every call from Delta's public tickers)."""
    try:
        rows = universe.top_liquid(universe.fetch_tickers(), max(1, min(n, MAX_SYMBOLS)))
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=502, detail="Delta public tickers are unreachable.") from None
    return {"symbols": rows, "ranked_by": "24 h turnover (USD) with a spread under 10 bps", "disclaimer": "Decision support, not investment advice."}


@router.post("/universe/parse")
def parse_batch(body: Batch, _: UserSession = Depends(require_verified_session), __: UserSession = Depends(require_csrf)) -> dict[str, Any]:
    try:
        return {"symbols": universe.parse_symbols(body.symbols)}
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from None


@router.get("")
def list_scanners(active_session: UserSession = Depends(require_verified_session), db: Session = Depends(get_db)) -> dict[str, Any]:
    rows = db.scalars(select(DeltaScanner).where(DeltaScanner.user_id == active_session.user_id).order_by(DeltaScanner.created_at.desc())).all()
    return {"items": [_json(r, full=False) for r in rows], "label": LABEL}


@router.post("")
def create_scanner(body: ScannerCreate, active_session: UserSession = Depends(require_verified_session), _: UserSession = Depends(require_csrf), db: Session = Depends(get_db)) -> dict[str, Any]:
    if body.strategy_key not in service.STRATEGIES or body.resolution not in RESOLUTIONS:
        raise HTTPException(status_code=422, detail="Unsupported strategy or resolution.")
    try:
        symbols = universe.parse_symbols(body.symbols) if body.symbols.strip() else []
        if body.top_liquid:
            symbols = list(dict.fromkeys(symbols + [r["symbol"] for r in universe.top_liquid(universe.fetch_tickers(), body.top_liquid)]))
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from None
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=502, detail="Delta public tickers are unreachable.") from None
    if not symbols or len(symbols) > MAX_SYMBOLS:
        raise HTTPException(status_code=422, detail=f"Choose between 1 and {MAX_SYMBOLS} symbols.")
    for s in symbols:
        try:
            fetch_spec(s)
        except CandleDataError as error:
            raise HTTPException(status_code=422, detail=f"{s}: {error}") from None
        except Exception:  # noqa: BLE001
            raise HTTPException(status_code=502, detail="Delta public contract service is unreachable.") from None
    now = datetime.now(timezone.utc)
    row = DeltaScanner(user_id=active_session.user_id, name=(body.name or f"Scanner {body.strategy_key} {body.resolution} ({len(symbols)})").strip(), strategy_key=body.strategy_key,
                       resolution=body.resolution, symbols=symbols, params={"ema": body.ema, "rr": str(body.rr), "direction": body.direction, "stop_buffer_pct": "0", "slippage_bps": str(body.slippage_bps)},
                       wallet_usd=body.wallet_usd, risk_pct=body.risk_pct, total_risk_cap_pct=body.total_risk_cap_pct, leverage_cap=body.leverage_cap, label="paper",
                       start_time=now, status="active", created_at=now, updated_at=now)
    db.add(row)
    db.commit()
    return _json(row, full=True)


@router.get("/{sid}")
def get_scanner(sid: UUID, active_session: UserSession = Depends(require_verified_session), db: Session = Depends(get_db)) -> dict[str, Any]:
    return _json(_owned(db, active_session.user_id, sid), full=True)


@router.post("/{sid}/refresh")
def refresh_scanner(sid: UUID, active_session: UserSession = Depends(require_verified_session), _: UserSession = Depends(require_csrf), db: Session = Depends(get_db)) -> dict[str, Any]:
    sc = _owned(db, active_session.user_id, sid)
    service.run_once(db, sc)
    return _json(sc, full=True)


@router.put("/{sid}/status")
def set_status(sid: UUID, status: str, active_session: UserSession = Depends(require_verified_session), _: UserSession = Depends(require_csrf), db: Session = Depends(get_db)) -> dict[str, Any]:
    if status not in ("active", "paused"):
        raise HTTPException(status_code=422, detail="Status must be active or paused.")
    sc = _owned(db, active_session.user_id, sid)
    sc.status, sc.updated_at = status, datetime.now(timezone.utc)
    db.commit()
    return _json(sc, full=False)


@router.delete("/{sid}")
def delete_scanner(sid: UUID, active_session: UserSession = Depends(require_verified_session), _: UserSession = Depends(require_csrf), db: Session = Depends(get_db)) -> dict[str, str]:
    db.delete(_owned(db, active_session.user_id, sid))
    db.commit()
    return {"status": "deleted"}


