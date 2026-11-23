"""Delta Exchange forward testing of the 5 EMA alert-candle strategy. PAPER ONLY: no order path to Delta exists.

The rule is the same as the equity library's `five_ema_alert` (services/indicators_more `ema_alert_candle`):
  sell alert: a completed candle whose low is above the EMA; it triggers when a later candle's low breaks the alert candle's low;
  buy alert mirrors it (high below the EMA, trigger when a later high breaks the alert's high); a later alert replaces an untriggered one.
Entry is a stop order at the alert level (a gap through it fills at the open), the stop is the alert candle's opposite extreme, the
target is `rr` times the stop distance. Only completed candles are used; a bar that touches both stop and target counts as stopped.
Fills pay adverse slippage and Delta's taker fee on both legs. GST on fees and funding are not modelled (stated in the assumptions).
Results are hypothetical and never mixed with real portfolio P&L.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.crypto_delta.candles import BASE_URL, RESOLUTION_SECONDS, SYMBOL_PATTERN, Candle, CandleDataError, load_candles, sync_candles
from app.crypto_delta.indicators import ema_series
from app.models.crypto_delta import CryptoForwardTest

ZERO, BPS = Decimal(0), Decimal(10000)
RESOLUTIONS = ("5m", "15m", "1h")
WARMUP_BARS = 300
LABEL = "Hypothetical paper test on completed Delta candles. Decision support, not investment advice."
MAX_TRADES_KEPT = 300


@dataclass(frozen=True)
class FiveEmaParams:
    ema: int = 5
    rr: Decimal = Decimal(3)
    direction: int = -1            # -1 sell only (the usual setup), 0 both, 1 buy only
    stop_buffer_pct: Decimal = ZERO


@dataclass(frozen=True)
class Spec:
    symbol: str
    contract_value: Decimal
    taker_fee: Decimal
    tick_size: Decimal | None


def fetch_spec(symbol: str, *, transport: httpx.BaseTransport | None = None) -> Spec:
    """Public contract data (no credentials). Fails closed rather than assuming a fee or contract size."""
    if not SYMBOL_PATTERN.match(symbol):
        raise CandleDataError("Invalid contract symbol.")
    with httpx.Client(timeout=20, transport=transport, headers={"User-Agent": "FinByteNexus/1.0 (research)"}) as client:
        response = client.get(f"{BASE_URL}/v2/products/{symbol}")
    if response.status_code != 200:
        raise CandleDataError(f"Delta contract lookup failed (HTTP {response.status_code}).")
    product = (json.loads(response.text, parse_float=Decimal).get("result") or {})
    quoting = (product.get("quoting_asset") or {}).get("symbol")
    if product.get("contract_type") != "perpetual_futures":
        raise CandleDataError(f"{symbol} is not a perpetual contract.")
    if quoting != "USD":
        raise CandleDataError("Only USD-quoted contracts are supported; no FX conversion is applied.")
    try:
        value, taker = Decimal(str(product["contract_value"])), Decimal(str(product["taker_commission_rate"]))
        tick = Decimal(str(product["tick_size"])) if product.get("tick_size") else None
    except (KeyError, ArithmeticError):
        raise CandleDataError("Contract value or fee rate is unavailable; refusing to assume it.") from None
    if value <= 0 or taker < 0:
        raise CandleDataError("Contract value or fee rate is unavailable; refusing to assume it.")
    return Spec(symbol, value, taker, tick)


def _trade(side: str, symbol: str, contracts: int, spec: Spec, signal_time: datetime, entry_time: datetime, exit_time: datetime | None,
           entry_raw: Decimal, entry_fill: Decimal, exit_raw: Decimal, exit_fill: Decimal, stop: Decimal, target: Decimal, reason: str) -> dict[str, Any]:
    units = Decimal(contracts) * spec.contract_value
    sign = Decimal(1) if side == "buy" else Decimal(-1)
    gross = (exit_raw - entry_raw) * units * sign
    slip = (abs(entry_fill - entry_raw) + abs(exit_fill - exit_raw)) * units
    fees = spec.taker_fee * (entry_fill + exit_fill) * units
    net = gross - slip - fees
    risk = abs(entry_fill - stop) * units
    return {"symbol": symbol, "side": side, "contracts": contracts, "signal_time": signal_time.isoformat(), "entry_time": entry_time.isoformat(),
            "exit_time": exit_time.isoformat() if exit_time else None, "entry_fill": str(entry_fill), "exit_fill": str(exit_fill), "stop_initial": str(stop),
            "target": str(target), "exit_reason": reason, "gross_pnl": str(gross), "slippage_cost": str(slip), "fees": str(fees), "net_pnl": str(net),
            "r_multiple": str(net / risk) if risk > 0 else None}


def replay(candles: list[Candle], symbol: str, params: FiveEmaParams, spec: Spec, contracts: int, slippage_bps: Decimal, start: datetime) -> dict[str, Any]:
    """Walk the completed candles once. Alerts are tracked from the first usable bar, but no trade is opened before `start`."""
    closes = [c.close for c in candles]
    ema = ema_series(closes, params.ema)
    slip = slippage_bps / BPS
    buf = params.stop_buffer_pct / Decimal(100)
    sell: tuple[Decimal, Decimal, datetime] | None = None   # (alert low, alert high, alert time)
    buy: tuple[Decimal, Decimal, datetime] | None = None    # (alert high, alert low, alert time)
    pos: dict[str, Any] | None = None
    closed: list[dict[str, Any]] = []

    def close_pos(bar: Candle, raw: Decimal, reason: str) -> None:
        nonlocal pos
        assert pos is not None
        adverse = Decimal(1) + slip if pos["side"] == "sell" else Decimal(1) - slip     # a short buys back higher, a long sells lower
        fill = raw * adverse
        closed.append(_trade(pos["side"], symbol, contracts, spec, pos["signal_time"], pos["entry_time"], bar.open_time, pos["entry_raw"], pos["entry_fill"], raw, fill,
                             pos["stop"], pos["target"], reason))
        pos = None

    for i, bar in enumerate(candles):
        e = ema[i]
        if e is None:
            continue
        if pos is not None:                                  # manage the open trade on bars after its entry bar
            if pos["side"] == "sell":
                if bar.high >= pos["stop"]:
                    close_pos(bar, max(bar.open, pos["stop"]), "stop")
                elif bar.low <= pos["target"]:
                    close_pos(bar, min(bar.open, pos["target"]), "target")
            else:
                if bar.low <= pos["stop"]:
                    close_pos(bar, min(bar.open, pos["stop"]), "stop")
                elif bar.high >= pos["target"]:
                    close_pos(bar, max(bar.open, pos["target"]), "target")
        sell_hit = sell is not None and bar.low < sell[0]
        buy_hit = buy is not None and bar.high > buy[0]
        for side, hit in (("sell", sell_hit), ("buy", buy_hit)):
            if not hit:
                continue
            alert = sell if side == "sell" else buy
            assert alert is not None
            if side == "sell":
                sell = None
            else:
                buy = None
            allowed = params.direction <= 0 if side == "sell" else params.direction >= 0
            if pos is not None or not allowed or bar.open_time < start:
                continue
            level, extreme, alert_time = alert
            if side == "sell":
                raw = min(bar.open, level)
                stop = extreme * (Decimal(1) + buf)
                fill = raw * (Decimal(1) - slip)
                if fill >= stop:
                    continue
                target = fill - params.rr * (stop - fill)
            else:
                raw = max(bar.open, level)
                stop = extreme * (Decimal(1) - buf)
                fill = raw * (Decimal(1) + slip)
                if fill <= stop:
                    continue
                target = fill + params.rr * (fill - stop)
            pos = {"side": side, "entry_raw": raw, "entry_fill": fill, "stop": stop, "target": target, "signal_time": alert_time, "entry_time": bar.open_time}
            # the entry bar itself: the stop is checked first (a bar touching both stop and target counts as stopped)
            if side == "sell":
                if bar.high >= stop:
                    close_pos(bar, stop, "stop")
                elif bar.low <= target:
                    close_pos(bar, target, "target")
            else:
                if bar.low <= stop:
                    close_pos(bar, stop, "stop")
                elif bar.high >= target:
                    close_pos(bar, target, "target")
        if bar.low > e and not sell_hit:
            sell = (bar.low, bar.high, bar.open_time)
        if bar.high < e and not buy_hit:
            buy = (bar.high, bar.low, bar.open_time)

    open_positions: list[dict[str, Any]] = []
    if pos is not None and candles:
        last = candles[-1]
        sign = Decimal(1) if pos["side"] == "buy" else Decimal(-1)
        units = Decimal(contracts) * spec.contract_value
        open_positions.append({"symbol": symbol, "side": pos["side"], "contracts": contracts, "entry_time": pos["entry_time"].isoformat(), "entry_fill": str(pos["entry_fill"]),
                               "stop_initial": str(pos["stop"]), "target": str(pos["target"]), "mark": str(last.close),
                               "unrealised_gross": str((last.close - pos["entry_raw"]) * units * sign)})
    return {"trades": closed, "open_positions": open_positions}


def summarise(trades: list[dict[str, Any]]) -> dict[str, Any]:
    total = lambda key: sum((Decimal(t[key]) for t in trades), ZERO)  # noqa: E731
    wins = [t for t in trades if Decimal(t["net_pnl"]) > 0]
    r = [Decimal(t["r_multiple"]) for t in trades if t["r_multiple"] is not None]
    return {"closed_trades": len(trades), "wins": len(wins), "losses": len(trades) - len(wins),
            "win_rate": round(len(wins) / len(trades), 4) if trades else None,
            "gross_pnl": str(total("gross_pnl")), "fees": str(total("fees")), "slippage": str(total("slippage_cost")), "net_pnl": str(total("net_pnl")),
            "avg_r": str(sum(r, ZERO) / len(r)) if r else None}


def _params_of(ft: CryptoForwardTest) -> FiveEmaParams:
    p = ft.params or {}
    return FiveEmaParams(int(p.get("ema", 5)), Decimal(str(p.get("rr", 3))), int(p.get("direction", -1)), Decimal(str(p.get("stop_buffer_pct", 0))))


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def run_once(db: Session, ft: CryptoForwardTest, now: datetime | None = None, *, transport: httpx.BaseTransport | None = None) -> dict[str, Any]:
    """Refresh public candles, replay causally, store the snapshot. Errors are stored, never turned into zeros."""
    now = _utc(now or datetime.now(timezone.utc))
    step = timedelta(seconds=RESOLUTION_SECONDS[ft.resolution])
    start = _utc(ft.start_time)
    window_start = start - step * WARMUP_BARS
    snapshot: dict[str, Any] = {"as_of": now.isoformat(), "label": LABEL, "paper_only": True, "symbol": ft.symbol, "resolution": ft.resolution,
                                "start_time": start.isoformat(), "contracts": ft.contracts, "params": ft.params, "slippage_bps": str(ft.slippage_bps)}
    try:
        spec = fetch_spec(ft.symbol, transport=transport)
        sync_candles(db, ft.symbol, ft.resolution, window_start, now, now=now, transport=transport)
        candles = load_candles(db, ft.symbol, ft.resolution, window_start, now, now=now)
        if len(candles) < _params_of(ft).ema + 2:
            raise CandleDataError("Not enough completed candles yet.")
        result = replay(candles, ft.symbol, _params_of(ft), spec, ft.contracts, ft.slippage_bps, start)
        last_close = candles[-1].open_time + step
        snapshot.update(summary=summarise(result["trades"]), trades=result["trades"][-MAX_TRADES_KEPT:], open_positions=result["open_positions"],
                        last_candle_close=last_close.isoformat(), bars=len(candles),
                        assumptions={"taker_fee_rate": str(spec.taker_fee), "contract_value": str(spec.contract_value), "fees": "taker fee on entry and exit, GST and funding not modelled",
                                     "fills": "stop-order entry at the alert level (gap fills at the open); stop wins a same-bar tie"})
        ft.last_error = None
    except (CandleDataError, httpx.HTTPError) as error:
        ft.last_error = str(error)[:400] or "Delta data unavailable."
        old = ft.snapshot or {}
        snapshot.update({k: old[k] for k in ("summary", "trades", "open_positions") if k in old}, error=ft.last_error)
    ft.snapshot, ft.last_run_at, ft.updated_at = snapshot, now, now
    db.commit()
    return snapshot


def due(ft: CryptoForwardTest, now: datetime) -> bool:
    """A decision only changes when a candle closes, so run once per candle (15 s after the close)."""
    if ft.status != "active":
        return False
    seconds = RESOLUTION_SECONDS[ft.resolution]
    boundary = datetime.fromtimestamp((int(now.timestamp()) // seconds) * seconds, tz=timezone.utc) + timedelta(seconds=15)
    last = _utc(ft.last_run_at) if ft.last_run_at else None
    return now >= boundary and (last is None or last < boundary)


def run_pass(db: Session, now: datetime | None = None) -> dict[str, int]:
    now = now or datetime.now(timezone.utc)
    ran = failed = 0
    for ft in list(db.scalars(select(CryptoForwardTest).where(CryptoForwardTest.status == "active"))):
        if due(ft, now):
            ran += 1
            failed += bool(run_once(db, ft, now).get("error"))
    return {"ran": ran, "failed": failed}
