"""Crypto (Delta Exchange India perpetuals) research API: candles, strategies, backtests.

Self-contained and separate from the Indian-equity system. Research only: nothing here places an
order. Backtest results are hypothetical, stored per owner, and never mixed with portfolio P&L.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.auth import get_active_session, require_csrf
from app.config import settings
from app.crypto_delta.backtest import MAX_BARS, ContractSpec, _plain, run_backtest
from app.crypto_delta.candles import (
    MAX_SYNC_DAYS, RESOLUTION_SECONDS, SYMBOL_PATTERN, CandleDataError, find_gaps, load_candles, sync_candles,
)
from app.crypto_delta.strategies import STRATEGIES, ParamError, resolve_params
from app.db import get_db
from app.delta_research.store import DeltaResearchStore
from app.models import UserSession
from app.models.crypto_delta import CryptoBacktestRun

router = APIRouter(prefix="/crypto-delta", tags=["crypto-delta"])
DISCLAIMER = "Decision support, not investment advice. Backtests are hypothetical."
Resolution = str


def require_verified_session(active_session: UserSession = Depends(get_active_session)) -> UserSession:
    if not active_session.user.email_verified:
        raise HTTPException(
            status_code=403,
            detail={"code": "email_verification_required", "message": "Verify your email address first."},
        )
    return active_session


def _decimal(value: Any) -> Decimal | None:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return number if number.is_finite() else None


def _contract_spec(symbol: str) -> ContractSpec:
    """Exchange-published contract data from the stored Delta product list. Fails closed."""
    if not SYMBOL_PATTERN.match(symbol):
        raise HTTPException(status_code=422, detail="Invalid contract symbol.")
    if not settings.delta_research_enabled or not settings.delta_research_db_path:
        raise HTTPException(status_code=503, detail="Delta product data is not available (research store disabled).")
    try:
        with DeltaResearchStore(settings.delta_research_db_path, read_only=True) as store:
            product_id = store.product_id_for_symbol(symbol)
            detail = store.product_detail(product_id, 1) if product_id else None
    except Exception:
        raise HTTPException(status_code=503, detail="Delta product data is unavailable.") from None
    if detail is None:
        raise HTTPException(status_code=404, detail=f"{symbol} is not a known Delta contract.")
    meta = detail["product"].get("metadata") or {}
    quoting = (meta.get("quoting_asset") or {}).get("symbol") if isinstance(meta.get("quoting_asset"), dict) else None
    contract_value, taker, maker = (_decimal(meta.get(k)) for k in ("contract_value", "taker_commission_rate", "maker_commission_rate"))
    if detail["product"].get("contract_type") != "perpetual_futures":
        raise HTTPException(status_code=422, detail=f"{symbol} is not a perpetual contract.")
    if quoting != "USD":
        raise HTTPException(status_code=409, detail="Only USD-quoted contracts are supported; no FX conversion is applied.")
    if contract_value is None or contract_value <= 0 or taker is None or taker < 0 or maker is None:
        raise HTTPException(status_code=409, detail="Contract value or fee rates are unavailable; refusing to assume them.")
    initial_margin, maintenance_margin = _decimal(meta.get("initial_margin")), _decimal(meta.get("maintenance_margin"))
    if initial_margin is None or initial_margin <= 0 or maintenance_margin is None or not 0 < maintenance_margin < initial_margin:
        raise HTTPException(status_code=409, detail="Margin data (initial/maintenance margin) is unavailable for this contract; refusing to assume it.")
    limit = _decimal(meta.get("position_size_limit"))
    return ContractSpec(
        symbol, contract_value, _decimal(meta.get("tick_size")), taker, maker,
        initial_margin_pct=initial_margin, maintenance_margin_pct=maintenance_margin, default_leverage=_decimal(meta.get("default_leverage")),
        liquidation_penalty_factor=_decimal(meta.get("liquidation_penalty_factor")),
        position_size_limit=int(limit) if limit is not None and limit > 0 else None,
    )


def _spec_json(spec: ContractSpec) -> dict[str, Any]:
    return {
        "symbol": spec.symbol, "contract_value": str(spec.contract_value),
        "tick_size": str(spec.tick_size) if spec.tick_size else None,
        "taker_fee_rate": str(spec.taker_fee), "maker_fee_rate": str(spec.maker_fee), "quote_currency": "USD",
        "units_per_contract": str(spec.contract_value),
        "initial_margin_pct": str(spec.initial_margin_pct) if spec.initial_margin_pct is not None else None,
        "maintenance_margin_pct": str(spec.maintenance_margin_pct) if spec.maintenance_margin_pct is not None else None,
        "max_leverage": _plain(spec.max_leverage) if spec.max_leverage else None,
        "default_leverage": _plain(spec.default_leverage) if spec.default_leverage else None,
        "liquidation_penalty_factor": str(spec.liquidation_penalty_factor) if spec.liquidation_penalty_factor is not None else None,
        "position_size_limit": spec.position_size_limit,
        "source": "Delta Exchange product data",
    }


def _resolution(value: str) -> str:
    if value not in RESOLUTION_SECONDS:
        raise HTTPException(status_code=422, detail=f"resolution must be one of {', '.join(RESOLUTION_SECONDS)}")
    return value


def _window(resolution: str, days: int) -> tuple[datetime, datetime]:
    if not 1 <= days <= MAX_SYNC_DAYS[resolution]:
        raise HTTPException(status_code=422, detail=f"days must be 1 to {MAX_SYNC_DAYS[resolution]} for {resolution} candles.")
    step = RESOLUTION_SECONDS[resolution]
    end = datetime.fromtimestamp(int(datetime.now(timezone.utc).timestamp()) // step * step, timezone.utc)
    return end - timedelta(days=days), end


class _Numbers(BaseModel):
    @field_validator("*", mode="before")
    @classmethod
    def no_floats(cls, value: Any) -> Any:
        if isinstance(value, float):
            raise ValueError("send numbers as strings or integers, never floats")
        return value


class SyncRequest(_Numbers):
    model_config = ConfigDict(extra="forbid")
    symbol: str
    resolution: Resolution
    days: int | None = Field(default=None, ge=1, le=1460)
    start: datetime | None = None  # with end: re-download exactly this window (e.g. a stored backtest's)
    end: datetime | None = None


class BacktestRequest(_Numbers):
    model_config = ConfigDict(extra="forbid")
    strategy_id: str
    symbol: str
    resolution: Resolution
    days: int = Field(ge=1, le=1460)
    params: dict[str, Any] = Field(default_factory=dict)
    contracts: Decimal = Field(default=Decimal("10"), gt=0, le=Decimal("1000000"))
    initial_capital: Decimal = Field(default=Decimal("1000"), ge=Decimal("100"), le=Decimal("10000000"))
    slippage_bps: Decimal = Field(default=Decimal("0"), ge=0, le=Decimal("100"))
    leverage: Decimal | None = Field(default=None, ge=1, le=1000)          # default: min(10x, the contract's maximum)
    sizing_mode: Literal["contracts", "notional_usd", "risk_pct"] = "contracts"
    notional_usd: Decimal | None = Field(default=None, gt=0, le=Decimal("100000000"))
    risk_pct: Decimal | None = Field(default=None, gt=0, le=100)
    window_end: datetime | None = None                       # pin the window end (a timezone-aware bar boundary) to reproduce a run exactly; default: the latest completed candle


def _quality(candles: list, resolution: str, start: datetime, end: datetime, extra: dict[str, Any]) -> dict[str, Any]:
    step = RESOLUTION_SECONDS[resolution]
    expected = int((end - start).total_seconds() // step)
    gaps = find_gaps(candles, resolution)
    return {
        "source": "Delta Exchange India public history (completed candles)",
        "bars": len(candles), "expected_bars": expected,
        "coverage_pct": str((Decimal(len(candles)) * 100 / expected).quantize(Decimal("0.1"))) if expected else None,
        "first_bar": candles[0].open_time.isoformat() if candles else None,
        "last_bar": candles[-1].open_time.isoformat() if candles else None,
        "gaps": len(gaps), "missing_bars": sum(g[2] for g in gaps),
        "largest_gap_bars": max((g[2] for g in gaps), default=0), **extra,
    }


def _run_json(row: CryptoBacktestRun, *, full: bool) -> dict[str, Any]:
    body = {
        "id": str(row.id), "created_at": row.created_at.isoformat(), "strategy_id": row.strategy_id,
        "strategy_name": STRATEGIES[row.strategy_id].name if row.strategy_id in STRATEGIES else row.strategy_id,
        "symbol": row.symbol, "resolution": row.resolution, "window_start": row.window_start.isoformat(),
        "window_end": row.window_end.isoformat(), "bars_used": row.bars_used, "status": row.status,
        "hypothetical": row.hypothetical, "metrics": row.metrics, "disclaimer": DISCLAIMER,
    }
    if full:
        body |= {"params": {k: str(v) for k, v in row.params.items()}, "assumptions": row.assumptions,
                 "data_quality": row.data_quality, "trades": row.trades, "equity": row.equity,
                 "label": "Hypothetical backtest on historical candles. Not a position, an order, or real P&L."}
    return body


@router.get("/strategies")
def list_strategies(_: UserSession = Depends(require_verified_session)) -> dict[str, Any]:
    return {
        "resolutions": list(RESOLUTION_SECONDS),
        "max_days": MAX_SYNC_DAYS,
        "strategies": [
            {
                "id": s.id, "name": s.name, "description": s.description,
                "params": [
                    {"key": p.key, "label": p.label, "kind": p.kind, "default": str(p.default).lower() if p.kind == "bool" else str(p.default),
                     "minimum": None if p.minimum is None else str(p.minimum), "maximum": None if p.maximum is None else str(p.maximum)}
                    for p in s.params
                ],
            }
            for s in STRATEGIES.values()
        ],
        "disclaimer": DISCLAIMER,
    }


@router.get("/contracts/{symbol}")
def contract_info(symbol: str, _: UserSession = Depends(require_verified_session)) -> dict[str, Any]:
    return _spec_json(_contract_spec(symbol.upper()))


@router.get("/candles")
def stored_candles(
    response: Response,
    symbol: str = Query(max_length=32),
    resolution: str = Query(default="15m"),
    limit: int = Query(default=500, ge=10, le=5000),
    start: datetime | None = Query(default=None, description="With end: return this exact window (e.g. a backtest's)."),
    end: datetime | None = Query(default=None),
    _: UserSession = Depends(require_verified_session),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    symbol = symbol.upper()
    _resolution(resolution)
    if not SYMBOL_PATTERN.match(symbol):
        raise HTTPException(status_code=422, detail="Invalid contract symbol.")
    now = datetime.now(timezone.utc)
    if (start is None) != (end is None):
        raise HTTPException(status_code=422, detail="Provide both start and end, or neither.")
    if start is not None and end is not None:
        if start.utcoffset() is None or end.utcoffset() is None or start >= end:
            raise HTTPException(status_code=422, detail="start and end must be timezone-aware with start before end.")
        candles = load_candles(db, symbol, resolution, start, end, now=now)[:limit]
    else:
        horizon = now - timedelta(seconds=RESOLUTION_SECONDS[resolution] * limit * 3)
        candles = load_candles(db, symbol, resolution, horizon, now + timedelta(seconds=1), now=now)[-limit:]
    return {
        "symbol": symbol, "resolution": resolution, "source": "delta_rest_history", "count": len(candles),
        "candles": [
            {"timestamp": c.open_time.isoformat(), "open": str(c.open), "high": str(c.high), "low": str(c.low),
             "close": str(c.close), "volume": str(c.volume)} for c in candles
        ],
        "gaps": len(find_gaps(candles, resolution)),
    }


@router.post("/candles/sync")
def sync(
    payload: SyncRequest,
    _: UserSession = Depends(require_verified_session),
    __: UserSession = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    symbol = payload.symbol.upper()
    _contract_spec(symbol)  # must be a known USD perpetual
    resolution = _resolution(payload.resolution)
    if payload.days is not None and (payload.start is not None or payload.end is not None):
        raise HTTPException(status_code=422, detail="Provide days, or both start and end, not both.")
    if payload.start is not None and payload.end is not None:
        start, end = payload.start, payload.end
        if start.utcoffset() is None or end.utcoffset() is None or start >= end:
            raise HTTPException(status_code=422, detail="start and end must be timezone-aware with start before end.")
        if (end - start) > timedelta(days=MAX_SYNC_DAYS[resolution]):
            raise HTTPException(status_code=422, detail=f"At most {MAX_SYNC_DAYS[resolution]} days of {resolution} candles per download.")
    elif payload.days is not None and payload.start is None and payload.end is None:
        start, end = _window(resolution, payload.days)
    else:
        raise HTTPException(status_code=422, detail="Provide days, or both start and end.")
    try:
        report = sync_candles(db, symbol, resolution, start, end)
    except CandleDataError as error:
        raise HTTPException(status_code=502, detail=str(error)) from None
    except Exception:
        raise HTTPException(status_code=502, detail="Delta public candle service is unreachable.") from None
    return {"symbol": symbol, "resolution": resolution, "window_start": start.isoformat(), "window_end": end.isoformat(), **report}


DEDUPE_SECONDS = 60


def _recent_identical_run(db: Session, user_id: Any, strategy_id: str, symbol: str, resolution: str, params: dict[str, Any], days: int, assumptions: dict[str, Any], now: datetime) -> Any:
    """The same owner's completed run with the same strategy, symbol, resolution, parameters, window length and sizing within the last 60 seconds (one click used to create three runs, and a moving window
    end made near-identical runs differ). Pass ``window_end`` to pin the window instead."""
    since = now - timedelta(seconds=DEDUPE_SECONDS)
    want = {k: str(v) for k, v in params.items()}
    span = timedelta(days=days)
    for row in db.scalars(select(CryptoBacktestRun).where(CryptoBacktestRun.user_id == user_id, CryptoBacktestRun.strategy_id == strategy_id, CryptoBacktestRun.symbol == symbol,
                                                           CryptoBacktestRun.resolution == resolution, CryptoBacktestRun.created_at >= since, CryptoBacktestRun.status == "completed").order_by(CryptoBacktestRun.created_at.desc())):
        if row.params == want and abs((row.window_end - row.window_start) - span) < timedelta(seconds=1) and row.assumptions == assumptions:       # same sizing, leverage, slippage and capital
            return row
    return None


@router.post("/backtests")
def create_backtest(
    payload: BacktestRequest,
    active_session: UserSession = Depends(require_verified_session),
    _: UserSession = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    strategy = STRATEGIES.get(payload.strategy_id)
    if strategy is None:
        raise HTTPException(status_code=404, detail="Unknown strategy.")
    symbol = payload.symbol.upper()
    spec = _contract_spec(symbol)
    resolution = _resolution(payload.resolution)
    try:
        params = resolve_params(strategy, payload.params)
    except ParamError as error:
        raise HTTPException(status_code=422, detail=str(error)) from None
    start, end = _window(resolution, payload.days)
    if payload.window_end is not None:
        if payload.window_end.tzinfo is None:
            raise HTTPException(status_code=422, detail="window_end must be timezone-aware.")
        step = RESOLUTION_SECONDS[resolution]
        end = datetime.fromtimestamp(int(payload.window_end.timestamp()) // step * step, timezone.utc)
        start = end - timedelta(days=payload.days)
    now = datetime.now(timezone.utc)
    expected = int((end - start).total_seconds() // RESOLUTION_SECONDS[resolution])
    if expected > MAX_BARS:
        raise HTTPException(status_code=422, detail=f"That window is {expected} bars; the limit is {MAX_BARS}. Use a coarser resolution or fewer days.")
    candles = load_candles(db, symbol, resolution, start, end, now=now)
    extra: dict[str, Any] = {"downloaded": False}
    if len(candles) < expected * 0.9:
        try:
            extra["download"] = sync_candles(db, symbol, resolution, start, end, now=now)
            extra["downloaded"] = True
        except Exception as error:  # use what is stored; the shortfall is reported, never hidden
            extra["download_error"] = str(error) if isinstance(error, CandleDataError) else "Delta public candle service is unreachable."
        candles = load_candles(db, symbol, resolution, start, end, now=now)
    minimum = strategy.warmup(params) + 30
    quality = _quality(candles, resolution, start, end, extra)
    base = dict(
        user_id=active_session.user_id, created_at=now, strategy_id=strategy.id, symbol=symbol, resolution=resolution,
        window_start=start, window_end=end, bars_used=len(candles), hypothetical=True, params={k: str(v) for k, v in params.items()},
        data_quality=quality,
    )
    if len(candles) < minimum:
        row = CryptoBacktestRun(**base, status="no_data", assumptions={}, metrics={}, trades=[], equity=[])
        db.add(row)
        db.commit()
        raise HTTPException(status_code=409, detail=f"Only {len(candles)} completed candles are available; this strategy needs at least {minimum}.")
    sizing_value = {"contracts": None, "notional_usd": payload.notional_usd, "risk_pct": payload.risk_pct}[payload.sizing_mode]
    if payload.sizing_mode != "contracts" and sizing_value is None:
        raise HTTPException(status_code=422, detail=f"sizing_mode {payload.sizing_mode} needs {payload.sizing_mode} to be set.")
    if payload.sizing_mode == "contracts" and spec.position_size_limit and payload.contracts > spec.position_size_limit:
        raise HTTPException(status_code=422, detail=f"{symbol} allows at most {spec.position_size_limit} contracts per position.")
    try:
        result = run_backtest(
            candles, resolution, strategy, params, spec,
            initial_capital=payload.initial_capital, contracts=payload.contracts, slippage_bps=payload.slippage_bps,
            leverage=payload.leverage, sizing_mode=payload.sizing_mode, sizing_value=sizing_value,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from None
    duplicate = _recent_identical_run(db, active_session.user_id, strategy.id, symbol, resolution, params, payload.days, result["assumptions"], now)
    if duplicate is not None:        # a double click or a retry: the identical run from the last 60 seconds is returned, no second row
        return {**_run_json(duplicate, full=True), "deduplicated": True}
    row = CryptoBacktestRun(**base, status="completed", assumptions=result["assumptions"], metrics=result["metrics"],
                            trades=result["trades"], equity=result["equity"])
    db.add(row)
    db.commit()
    db.refresh(row)
    return _run_json(row, full=True)


@router.get("/backtests")
def list_backtests(
    limit: int = Query(default=30, ge=1, le=100),
    active_session: UserSession = Depends(require_verified_session),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    rows = db.scalars(
        select(CryptoBacktestRun).where(CryptoBacktestRun.user_id == active_session.user_id)
        .order_by(CryptoBacktestRun.created_at.desc()).limit(limit)
    ).all()
    return [_run_json(row, full=False) for row in rows]


@router.delete("/backtests/{run_id}", status_code=204)
def delete_backtest(
    run_id: UUID,
    active_session: UserSession = Depends(require_verified_session),
    _: UserSession = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> Response:
    """Delete one of the caller's stored (hypothetical) backtest runs. Owner-scoped; nothing else is touched."""
    row = db.scalar(select(CryptoBacktestRun).where(CryptoBacktestRun.id == run_id, CryptoBacktestRun.user_id == active_session.user_id))
    if row is None:
        raise HTTPException(status_code=404, detail="Backtest not found.")
    db.delete(row)
    db.commit()
    return Response(status_code=204)


@router.get("/backtests/{run_id}")
def get_backtest(
    run_id: UUID,
    active_session: UserSession = Depends(require_verified_session),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    row = db.scalar(select(CryptoBacktestRun).where(
        CryptoBacktestRun.id == run_id, CryptoBacktestRun.user_id == active_session.user_id))
    if row is None:
        raise HTTPException(status_code=404, detail="Backtest not found.")
    return _run_json(row, full=True)
