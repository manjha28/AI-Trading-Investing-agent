"""Run a scanner: replay the strategy on every symbol's completed candles, merge the signals into one queue, size them with the risk rules, store the snapshot.
PAPER ONLY. This module imports nothing from app.delta_execution and never builds an order client."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.crypto_delta.candles import RESOLUTION_SECONDS, CandleDataError, load_candles, sync_candles
from app.crypto_delta.forward import WARMUP_BARS, FiveEmaParams, fetch_spec, replay
from app.delta_scanner.portfolio import RiskRules, simulate
from app.models.delta_scanner import DeltaScanner

STRATEGIES = ("five_ema_alert",)


def _utc(v: datetime) -> datetime:
    return v if v.tzinfo else v.replace(tzinfo=timezone.utc)


def run_once(db: Session, sc: DeltaScanner, now: datetime | None = None, *, transport: httpx.BaseTransport | None = None) -> dict[str, Any]:
    now = _utc(now or datetime.now(timezone.utc))
    step = timedelta(seconds=RESOLUTION_SECONDS[sc.resolution])
    start = _utc(sc.start_time)
    p = sc.params or {}
    params = FiveEmaParams(int(p.get("ema", 5)), Decimal(str(p.get("rr", 3))), int(p.get("direction", -1)), Decimal(str(p.get("stop_buffer_pct", 0))))
    per_symbol: dict[str, dict[str, Any]] = {}
    errors: dict[str, str] = {}
    open_now: list[dict[str, Any]] = []
    for symbol in sc.symbols:
        try:
            spec = fetch_spec(symbol, transport=transport)
            sync_candles(db, symbol, sc.resolution, start - step * WARMUP_BARS, now, now=now, transport=transport)
            candles = load_candles(db, symbol, sc.resolution, start - step * WARMUP_BARS, now, now=now)
            if len(candles) < params.ema + 2:
                raise CandleDataError("Not enough completed candles yet.")
            result = replay(candles, symbol, params, spec, 1, Decimal(str(p.get("slippage_bps", 5))), start)
            per_symbol[symbol] = {"contract_value": spec.contract_value, "trades": result["trades"]}
            open_now += [{"symbol": symbol, **o} for o in result["open_positions"]]
        except (CandleDataError, httpx.HTTPError) as error:
            errors[symbol] = str(error)[:200] or "Delta data unavailable."
    rules = RiskRules(Decimal(sc.wallet_usd), Decimal(sc.risk_pct), Decimal(sc.leverage_cap), Decimal(sc.total_risk_cap_pct))
    snap = simulate(per_symbol, rules)
    snap.update(as_of=now.isoformat(), strategy_key=sc.strategy_key, resolution=sc.resolution, symbols=list(sc.symbols), errors=errors, open_signals=open_now[-50:],
                symbols_ok=len(per_symbol), label_kind=sc.label)
    sc.snapshot, sc.last_run_at, sc.updated_at = snap, now, now
    sc.last_error = (f"{len(errors)} of {len(sc.symbols)} symbols had no data" if errors else None)
    db.commit()
    return snap


def due(sc: DeltaScanner, now: datetime) -> bool:
    if sc.status != "active":
        return False
    seconds = RESOLUTION_SECONDS[sc.resolution]
    boundary = datetime.fromtimestamp((int(now.timestamp()) // seconds) * seconds, tz=timezone.utc) + timedelta(seconds=30)
    last = _utc(sc.last_run_at) if sc.last_run_at else None
    return now >= boundary and (last is None or last < boundary)


def run_pass(db: Session, now: datetime | None = None) -> dict[str, int]:
    from sqlalchemy import select
    now = now or datetime.now(timezone.utc)
    ran = 0
    for sc in list(db.scalars(select(DeltaScanner).where(DeltaScanner.status == "active"))):
        if due(sc, now):
            ran += 1
            run_once(db, sc, now)
    return {"ran": ran}
