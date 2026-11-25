"""Paper-only trend candidates for the Delta scanner (owner, 25 Nov 2026). Frozen rules; hypothetical results on completed Delta candles only.

  trend_donchian_4h  4-hour bars: long when the close is above the highest high of the prior 50 bars, short below the lowest low; exit on the opposite
                     25-bar channel or a 3 x ATR(14) trailing stop from the best close. (The other session's best candidate, 25 Nov 2026.)
  momentum_56d       daily bars: hold the sign of the 56-day return (long if up, short if down); switch when the sign flips. Sizing stop 3 x ATR(14).
  rsi2_daily         daily bars (Connors RSI(2) 5/95): long when RSI(2) < 5 and the close is above its 200-day average, short when RSI(2) > 95 and below
                     it; exit when the close crosses the 5-day average. Sizing stop 3 x ATR(14).
Entries and exits fill at the NEXT bar's open (completed bars only). Net P&L of one contract pays Delta's taker fee + 18% GST on both fills.
None of these is on the live allowlist: a scanner running them stays paper only."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import numpy as np

from app.crypto_delta.candles import CandleDataError, load_candles, sync_candles

TREND_STRATEGIES = ("trend_donchian_4h", "momentum_56d", "rsi2_daily")
GST = Decimal("1.18")


def _arrays(candles):
    t = [c.open_time for c in candles]
    o, h, lo, c = (np.array([float(getattr(x, k)) for x in candles]) for k in ("open", "high", "low", "close"))
    pc = np.r_[c[0], c[:-1]]
    tr = np.maximum(h - lo, np.maximum(abs(h - pc), abs(lo - pc)))
    atr = np.zeros(len(c))
    for i in range(len(c)):
        atr[i] = tr[i] if i == 0 else atr[i - 1] + (tr[i] - atr[i - 1]) / 14
    return t, o, h, lo, c, atr


def _rsi(c: np.ndarray, n: int) -> np.ndarray:
    d = np.diff(c, prepend=c[0])
    up = np.zeros(len(c)); dn = np.zeros(len(c))
    for i in range(1, len(c)):
        up[i] = up[i - 1] + (max(d[i], 0) - up[i - 1]) / n
        dn[i] = dn[i - 1] + (max(-d[i], 0) - dn[i - 1]) / n
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(dn > 0, 100 - 100 / (1 + up / dn), 100.0)


def positions(key: str, o, h, lo, c, atr) -> list[tuple[int, int, int | None, float]]:
    """(entry bar, side, exit bar or None while open, sizing stop) for every trade; decisions at a bar's close, fills at the next bar's open."""
    n, out = len(c), []
    side, start, best, stop = 0, None, 0.0, 0.0
    ma200 = np.array([c[max(0, i - 199):i + 1].mean() if i >= 199 else np.nan for i in range(n)])
    ma5 = np.array([c[max(0, i - 4):i + 1].mean() for i in range(n)])
    r2 = _rsi(c, 2)
    for i in range(60, n - 1):
        want = side
        if key == "trend_donchian_4h":
            hi50, lo50, hi25, lo25 = h[i - 50:i].max(), lo[i - 50:i].min(), h[i - 25:i].max(), lo[i - 25:i].min()
            if side != 0:
                best = max(best, c[i]) if side > 0 else min(best, c[i])
                trail = best - 3 * atr[i] if side > 0 else best + 3 * atr[i]
                if (side > 0 and (c[i] < lo25 or c[i] < trail)) or (side < 0 and (c[i] > hi25 or c[i] > trail)):
                    want = 0
            if want == 0 and side == 0:
                want = 1 if c[i] > hi50 else -1 if c[i] < lo50 else 0
        elif key == "momentum_56d":
            if i >= 56:
                want = 1 if c[i] > c[i - 56] else -1
        else:
            if not np.isfinite(ma200[i]):
                continue
            if side > 0 and c[i] > ma5[i]:
                want = 0
            elif side < 0 and c[i] < ma5[i]:
                want = 0
            elif side == 0:
                want = 1 if (r2[i] < 5 and c[i] > ma200[i]) else -1 if (r2[i] > 95 and c[i] < ma200[i]) else 0
        if want != side:
            if side != 0 and start is not None:
                out.append((start, side, i + 1, stop))
            side, start = want, (i + 1 if want != 0 else None)
            best = o[i + 1] if want != 0 else 0.0
            stop = (o[i + 1] - want * 3 * atr[i]) if want != 0 else 0.0
    if side != 0 and start is not None:
        out.append((start, side, None, stop))
    return out


def trades(db, key: str, symbol: str, spec, start: datetime, now: datetime, transport=None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    res = "4h" if key == "trend_donchian_4h" else "1d"
    step = timedelta(hours=4) if res == "4h" else timedelta(days=1)
    warm = step * (260 if res == "1d" else 120)
    sync_candles(db, symbol, res, start - warm, now, now=now, transport=transport)
    candles = load_candles(db, symbol, res, start - warm, now, now=now)
    if len(candles) < (210 if res == "1d" else 80):
        raise CandleDataError(f"Not enough completed {res} candles yet.")
    t, o, h, lo, c, atr = _arrays(candles)
    cv, fee = spec.contract_value, spec.taker_fee * GST
    closed, open_ = [], []
    first = next((k for k, x in enumerate(t) if x >= start), None)
    for a, side, b, stop in positions(key, o, h, lo, c, atr):
        if t[a] < start:
            # a position already running when the test started is joined at the first bar after the start (a forward test never back-dates an entry)
            if first is None or (b is not None and b <= first):
                continue
            a = first
            stop = o[a] - side * 3 * atr[a - 1]
        entry = Decimal(str(o[a]))
        row = {"symbol": symbol, "side": "buy" if side > 0 else "sell", "signal_time": (t[a] - step).isoformat(), "entry_time": t[a].isoformat(),
               "entry_fill": str(entry), "stop_initial": str(Decimal(str(round(stop, 8))))}
        if b is None:
            open_.append({**row, "mark": str(c[-1])})
            continue
        exit_ = Decimal(str(o[b]))
        net = (exit_ - entry) * cv * side - (entry + exit_) * cv * fee
        closed.append({**row, "exit_time": t[b].isoformat(), "exit_fill": str(exit_), "net_pnl": str(net), "exit_reason": "rule"})
    return closed, open_


def utc(v: datetime) -> datetime:
    return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
