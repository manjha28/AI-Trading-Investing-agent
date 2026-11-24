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

STRATEGIES = ("five_ema_alert", "funding_extreme")
FUNDING_PARAMS = (0.9, 12, 48, 2.5)        # frozen: funding percentile, stall bars, hold bars, stop in ATR (Part D2 finalist, hash 00bd99ec7d9baf79). UNPROVEN: it did not pass the pass rule.


def _utc(v: datetime) -> datetime:
    return v if v.tzinfo else v.replace(tzinfo=timezone.utc)


def _funding_trades(db: Session, symbol: str, spec, start: datetime, now: datetime, transport) -> list[dict[str, Any]]:
    """Unproven Part D2 candidate on 1-hour bars: funding in its top decile with price stalling -> short (mirror for long), taker entry at the next open, 48 h hold,
    2.5 ATR stop. Returns trades in the replay format with 1 contract. Only trades entered after `start` count."""
    import pandas as pd

    from app.delta_lab import data, round4_d2 as d2

    step = timedelta(hours=1)
    sync_candles(db, symbol, "1h", start - step * 24 * 60, now, now=now, transport=transport)
    candles = load_candles(db, symbol, "1h", start - step * 24 * 60, now, now=now)
    if len(candles) < 24 * 40:
        raise CandleDataError("Not enough completed 1-hour candles yet.")
    df = pd.DataFrame({"time": [int(c.open_time.timestamp()) for c in candles], "open": [float(c.open) for c in candles], "high": [float(c.high) for c in candles],
                       "low": [float(c.low) for c in candles], "close": [float(c.close) for c in candles], "volume": [float(c.volume) for c in candles]})
    fd = data.fetch_candles(f"FUNDING:{symbol}", "1h", int((start - timedelta(days=60)).timestamp()), int(now.timestamp()))
    if len(fd) < 24 * 30:
        raise CandleDataError("Funding history is too short.")
    s = d2.sym_from_frames(symbol, df, fd)
    trades, _ = d2.run(s, d2.f2_funding(s, *FUNDING_PARAMS), "taker")
    cv = float(spec.contract_value)
    out = []
    for t in trades:
        if t["t_in"] < int(start.timestamp()):
            continue
        usd = t["net_bps"] / 1e4 * t["entry_px"] * cv                       # net P&L of one contract
        out.append({"symbol": symbol, "side": "buy" if t["side"] > 0 else "sell", "signal_time": datetime.fromtimestamp(t["t"], tz=timezone.utc).isoformat(),
                    "entry_time": datetime.fromtimestamp(t["t_in"], tz=timezone.utc).isoformat(), "exit_time": datetime.fromtimestamp(t["t_out"], tz=timezone.utc).isoformat(),
                    "entry_fill": str(t["entry_px"]), "stop_initial": str(t["stop"]), "net_pnl": str(round(usd, 8)), "exit_reason": t["reason"]})
    return out


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
            if sc.strategy_key == "funding_extreme":
                per_symbol[symbol] = {"contract_value": spec.contract_value, "trades": _funding_trades(db, symbol, spec, start, now, transport)}
                continue
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
