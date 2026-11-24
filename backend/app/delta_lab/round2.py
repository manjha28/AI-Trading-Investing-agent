"""Round 2: Binance-derived signals (R1-R5), executed with the Delta cost model and the maker-first simulator. R6/R0 (1-second ticks) live in round2_ticks.py.
Same risk gate, cost model and pass bar as round 1 (see run.py); walk-forward is 12-month train / 3-month test rolling quarterly on the first 75%.
Hypothetical research; decision support, not investment advice."""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from . import families
from .engine import Leg, Market, Signal
from .risk import ContractSpec

BIN = Path(__file__).resolve().parents[3] / "data" / "binance"
FOUR_H = "4h"


def _atr_grid(m: Market, rule: str, n: int = 14) -> np.ndarray:
    bars = families.resample(m, rule)
    atr = families._atr(bars, n)
    secs = {"1h": 3600, "4h": 14400}[rule]
    ends = families._epoch_s(bars.index) + secs
    pos = np.searchsorted(ends, m.t + 60, side="right") - 1
    out = np.full(len(m.t), np.nan)
    ok = pos >= 0
    out[ok] = atr.to_numpy()[pos[ok]]
    return out


def build_binance_market(symbol: str, delta_symbol: str) -> Market:
    """Binance USDT-perp price series with the DELTA contract spec (contract value, margins, fees)."""
    last = pd.read_pickle(BIN / f"{symbol}_last_1m.pkl").drop_duplicates("time").sort_values("time").reset_index(drop=True)
    mark = pd.read_pickle(BIN / f"{symbol}_mark_1m.pkl").drop_duplicates("time").set_index("time")
    fund = pd.read_pickle(BIN / f"{symbol}_funding.pkl")
    t = last["time"].to_numpy(np.int64)
    mk = mark.reindex(t).ffill().bfill()
    spec, tick, cv = families.spec_from_json(delta_symbol)
    funding = {int(ts): float(r) for ts, r in zip(fund["time"], fund["rate"])}
    # Binance funding is stamped a few ms after the 8-hour mark: round to the exact funding time
    funding = {(ts + 30) // 28800 * 28800: r for ts, r in funding.items()}
    m = Market(delta_symbol, spec, tick, cv, t, last["open"].to_numpy(float), last["high"].to_numpy(float), last["low"].to_numpy(float), last["close"].to_numpy(float),
               last["volume"].to_numpy(float), mk["open"].to_numpy(float), mk["high"].to_numpy(float), mk["low"].to_numpy(float), mk["close"].to_numpy(float), funding)
    m.atr_1h = _atr_grid(m, "1h")
    m.atr_4h = _atr_grid(m, "4h")
    m.funding_hist = fund
    return m


def _idx_at(m: Market, ts: int) -> int:
    """Index of the 1m bar whose CLOSE is at ts (bar opens at ts-60); -1 if that minute is missing."""
    i = int(np.searchsorted(m.t, ts - 60, side="right") - 1)
    return i if i >= 0 and m.t[i] == ts - 60 else -1


# ------------------------------ families ------------------------------
GRIDS2 = {
    "R1": [(p, a, h, n) for p in (5, 10) for a in (2, 3) for h in (24, 48, 72) for n in (2, 10)],
    "R2": [(nn, mm) for nn in (20, 55) for mm in (10, 20)],
    "R3": [("3R/48h",)],
    "R4": [(kind, n) for kind in ("weekend", "us_session") for n in (2, 10)],
    "R5": [(w, z, n) for w in (72, 168) for z in (2.0, 2.5) for n in (2, 10)],
}


def r1(m: Market, p: int, a: float, hold_h: int, n_min: int):
    fh = m.funding_hist
    rate = fh["rate"].astype(float).reset_index(drop=True)
    rank = rate.rolling(90).rank(pct=True)                        # 30 days of 8-hour funding
    out = []
    for i in np.flatnonzero(((rank >= 1 - p / 100) | (rank <= p / 100)).fillna(False).to_numpy()):
        ts = (int(fh["time"].iloc[i]) + 30) // 28800 * 28800 + 180   # decide 3 minutes after the funding time (the gate refuses inside 2 minutes)
        idx = _idx_at(m, ts)
        if idx < 0 or not np.isfinite(m.atr_4h[idx]):
            continue
        crowd_long = rank.iloc[i] >= 1 - p / 100 and rank.iloc[i] > 0.5
        px = m.c[idx]
        atr = m.atr_4h[idx]
        leg = Leg("short", px, px + a * atr, None) if crowd_long else Leg("long", px, px - a * atr, None)
        out.append(Signal(idx, "R1", f"R1|pct={p}|atr4h={a}|hold={hold_h}h|N={n_min}m", [leg], "limit", valid_bars=n_min, max_hold=hold_h * 60, reprice=True))
    return out


def r2(m: Market, nn: int, mm: int):
    bars = families.resample(m, FOUR_H)
    starts = families._epoch_s(bars.index)
    hi, lo, cl = bars["h"].to_numpy(), bars["l"].to_numpy(), bars["c"].to_numpy()
    hiN = bars["h"].rolling(nn).max().shift(1).to_numpy(); loN = bars["l"].rolling(nn).min().shift(1).to_numpy()
    hiM = bars["h"].rolling(mm).max().shift(1).to_numpy(); loM = bars["l"].rolling(mm).min().shift(1).to_numpy()
    out, busy_to = [], -1
    for i in range(max(nn, mm) + 1, len(bars) - 1):
        if i <= busy_to:
            continue
        side = "long" if cl[i] > hiN[i] else "short" if cl[i] < loN[i] else None
        if side is None:
            continue
        idx = _idx_at(m, int(starts[i]) + 14400)
        if idx < 0 or not np.isfinite(m.atr_4h[idx]):
            continue
        j = i + 1                                                   # exit: a 4h close through the opposite M-bar channel
        while j < len(bars) - 1 and j - i < 540 and not ((side == "long" and cl[j] < loM[j]) or (side == "short" and cl[j] > hiM[j])):
            j += 1
        px, atr = m.c[idx], m.atr_4h[idx]
        leg = Leg("long", px, px - 2.5 * atr, None) if side == "long" else Leg("short", px, px + 2.5 * atr, None)
        out.append(Signal(idx, "R2", f"R2|donchian={nn}|exit={mm}", [leg], "market", max_hold=(j - i) * 240))
        busy_to = j
    return out


def r3(m: Market, _x):
    bars = families.resample(m, "1h")
    hi24, lo24 = bars["h"].rolling(24).max(), bars["l"].rolling(24).min()
    rank = (hi24 - lo24).rolling(1440).rank(pct=True)
    starts = families._epoch_s(bars.index)
    out = []
    for i in np.flatnonzero((rank <= 0.10).fillna(False).to_numpy()):
        idx = _idx_at(m, int(starts[i]) + 3600)
        if idx < 0:
            continue
        hi, lo = float(hi24.iloc[i]), float(lo24.iloc[i])
        legs = [Leg("long", hi, lo, hi + 3 * (hi - lo)), Leg("short", lo, hi, lo - 3 * (hi - lo))]
        out.append(Signal(idx, "R3", "R3|3R/48h", legs, "stop_entry", valid_bars=60, max_hold=48 * 60))
    return out


def r4(m: Market, kind: str, n_min: int):
    out = []
    days = np.arange(m.t[0] - m.t[0] % 86400 + 86400, m.t[-1] - 4 * 86400, 86400, dtype=np.int64)
    for ds in days:
        wd = datetime.fromtimestamp(int(ds), tz=timezone.utc).weekday()
        if kind == "weekend" and wd == 4:
            idx = _idx_at(m, int(ds) + 20 * 3600); i0 = _idx_at(m, int(ds))
            if idx < 0 or i0 < 0 or not np.isfinite(m.atr_4h[idx]):
                continue
            move = m.c[idx] - m.c[i0 - 1] if i0 > 0 else 0.0
            if move == 0:
                continue
            hold = (4 + 48) * 60
        elif kind == "us_session" and wd < 5 and m.symbol == "BTCUSD":
            idx = _idx_at(m, int(ds) + 13 * 3600 + 1800)
            if idx < 0 or not np.isfinite(m.atr_4h[idx]):
                continue
            move, hold = 1.0, 390
        else:
            continue
        px, atr = m.c[idx], m.atr_4h[idx]
        leg = Leg("long", px, px - 3 * atr, None) if move > 0 else Leg("short", px, px + 3 * atr, None)
        out.append(Signal(idx, "R4", f"R4|{kind}|N={n_min}m", [leg], "limit", valid_bars=n_min, max_hold=hold, reprice=True))
    return out


def r5_signals(btc: Market, eth: Market, window: int, z_in: float, n_min: int):
    """Hourly z-score of log(ETH/BTC). Returns ({'BTCUSD': [...], 'ETHUSD': [...]}) leg signals; exit at z=0 or 72 h."""
    b, e = families.resample(btc, "1h")["c"], families.resample(eth, "1h")["c"]
    df = pd.concat([b.rename("b"), e.rename("e")], axis=1).dropna()
    lr = np.log(df["e"] / df["b"])
    z = ((lr - lr.rolling(window).mean()) / lr.rolling(window).std()).to_numpy()
    starts = families._epoch_s(df.index)
    out = {"BTCUSD": [], "ETHUSD": []}
    busy_to = -1
    for i in range(window, len(df) - 1):
        if i <= busy_to or not np.isfinite(z[i]) or abs(z[i]) <= z_in:
            continue
        ib, ie = _idx_at(btc, int(starts[i]) + 3600), _idx_at(eth, int(starts[i]) + 3600)
        if ib < 0 or ie < 0 or not (np.isfinite(btc.atr_4h[ib]) and np.isfinite(eth.atr_4h[ie])):
            continue
        j = i + 1
        while j < len(df) - 1 and j - i < 72 and (z[j] * np.sign(z[i]) > 0):
            j += 1
        eth_cheap = z[i] < 0                                         # ETH cheap vs BTC: long ETH, short BTC
        for m, idx, long_leg in ((eth, ie, eth_cheap), (btc, ib, not eth_cheap)):
            px, atr = m.c[idx], m.atr_4h[idx]
            leg = Leg("long", px, px - 3 * atr, None) if long_leg else Leg("short", px, px + 3 * atr, None)
            out[m.symbol].append(Signal(idx, "R5", f"R5|z{window}h|in={z_in}|N={n_min}m", [leg], "limit", valid_bars=n_min, max_hold=(j - i) * 60, reprice=True))
        busy_to = j
    return out


def variant_names(fam: str, grid) -> str:
    return {"R1": lambda g: f"R1|pct={g[0]}|atr4h={g[1]}|hold={g[2]}h|N={g[3]}m", "R2": lambda g: f"R2|donchian={g[0]}|exit={g[1]}", "R3": lambda g: "R3|3R/48h",
            "R4": lambda g: f"R4|{g[0]}|N={g[1]}m", "R5": lambda g: f"R5|z{g[0]}h|in={g[1]}|N={g[2]}m"}[fam](grid)
