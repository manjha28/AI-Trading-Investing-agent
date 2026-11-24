"""The six pre-registered families and their fixed grids. Signals use only data completed before the decision (no look-ahead).
Count of variants is reported by run.py. Hypothetical research; decision support, not investment advice."""
from __future__ import annotations

import json
from decimal import Decimal as D

import numpy as np
import pandas as pd

from . import data
from .engine import Leg, Market, Signal
from .risk import ContractSpec

GRIDS = {
    "F1": [(p, k) for p in (10, 20) for k in (1.5, 2.5)],
    "F2": [(s, mm, hold) for s in (4, 6) for mm in (0.3, 0.6) for hold in (30, 90)],
    "F3": [(p, a, hold) for p in (5, 10) for a in (2, 3) for hold in (8, 24)],
    "F4": [(lb,) for lb in (4, 12, 24)],
    "F5": [(prior, hold) for prior in (30, 60) for hold in (30, 120)],
    "F6": [("proxy",)],
}
F5_STOP_ATR = 1.5      # F5 and F6 have no stop in the brief; the gate needs one, so it is fixed here before any result is seen
F6_STOP_ATR = 1.0


def spec_from_json(symbol: str) -> tuple[ContractSpec, float, float]:
    raw = json.loads((data.DATA_DIR / f"{symbol}_spec.json").read_text())
    spec = ContractSpec(symbol, D(raw["contract_value"]), D(raw["tick_size"]), mm_pct=D(raw["maintenance_margin"]) / 100, im_pct_min=D(raw["initial_margin"]) / 100,
                        slope_threshold_notional=D(raw["max_leverage_notional"]), max_leverage=D(1) / (D(raw["initial_margin"]) / 100),
                        taker_fee=D(raw["taker_commission_rate"]), maker_fee=D(raw["maker_commission_rate"]))
    return spec, float(raw["tick_size"]), float(raw["contract_value"])


def build_market(symbol: str, series_prefix: str = "") -> Market:
    """`series_prefix` "live_" reads the rolling recent series the paper engine keeps; "" reads the research download."""
    last = data.load(f"{symbol}_{series_prefix}last_1m").sort_values("time").drop_duplicates("time").reset_index(drop=True)
    mark = data.load(f"{symbol}_{series_prefix}mark_1m").sort_values("time").drop_duplicates("time").set_index("time")
    t = last["time"].to_numpy(dtype=np.int64)
    mk = mark.reindex(t).ffill().bfill()
    spec, tick, cv = spec_from_json(symbol)
    fund = data.load(f"{symbol}_{series_prefix}funding_1h").sort_values("time").drop_duplicates("time")
    # the rate paid at funding time T is the FUNDING candle of the hour ending at T (known before T: no look-ahead). The series is in percent.
    funding = {int(ts) + 3600: float(c) / 100.0 for ts, c in zip(fund["time"], fund["close"]) if (int(ts) + 3600) % 28800 == 0}
    m = Market(symbol, spec, tick, cv, t, last["open"].to_numpy(float), last["high"].to_numpy(float), last["low"].to_numpy(float), last["close"].to_numpy(float), last["volume"].fillna(0).to_numpy(float),
               mk["open"].to_numpy(float), mk["high"].to_numpy(float), mk["low"].to_numpy(float), mk["close"].to_numpy(float), funding)
    m.atr_1h = _atr_1h_on_grid(m)
    m.funding_df = fund.reset_index(drop=True)
    return m


def frame(m: Market) -> pd.DataFrame:
    return pd.DataFrame({"o": m.o, "h": m.h, "l": m.l, "c": m.c, "v": m.v}, index=pd.to_datetime(m.t, unit="s", utc=True))


def resample(m: Market, rule: str) -> pd.DataFrame:
    df = frame(m)
    out = df.resample(rule, label="left", closed="left").agg({"o": "first", "h": "max", "l": "min", "c": "last", "v": "sum"}).dropna()
    return out


def _epoch_s(index: pd.DatetimeIndex) -> np.ndarray:
    return index.as_unit("s").asi8.astype(np.int64)


def _atr(bars: pd.DataFrame, n: int = 14) -> pd.Series:
    pc = bars["c"].shift(1)
    tr = pd.concat([bars["h"] - bars["l"], (bars["h"] - pc).abs(), (bars["l"] - pc).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean()


def _atr_1h_on_grid(m: Market) -> np.ndarray:
    bars = resample(m, "1h")
    atr = _atr(bars).shift(0)                                   # value for the hour that has just completed
    ends = _epoch_s(bars.index) + 3600   # known from the end of that hour
    pos = np.searchsorted(ends, m.t + 60, side="right") - 1      # at the close of 1m bar i
    out = np.full(len(m.t), np.nan)
    ok = pos >= 0
    out[ok] = atr.to_numpy()[pos[ok]]
    return out


def _idx_of_close(m: Market, bar_starts: np.ndarray, length_s: int) -> np.ndarray:
    return np.searchsorted(m.t, bar_starts + length_s - 60, side="right") - 1


def f1(m: Market, p: int, k: float):
    bars = resample(m, "15min")
    hi20 = bars["h"].rolling(20).max()
    lo20 = bars["l"].rolling(20).min()
    rank = (hi20 - lo20).rolling(2880).rank(pct=True)
    starts = _epoch_s(bars.index)
    idxs = _idx_of_close(m, starts, 900)
    out = []
    for i in np.flatnonzero((rank <= p / 100).to_numpy()):
        a = m.atr_1h[idxs[i]]
        if not np.isfinite(a) or a <= 0:
            continue
        hi, lo = float(hi20.iloc[i]), float(lo20.iloc[i])
        sl = max(lo, hi - k * a)
        ss = min(hi, lo + k * a)
        legs = [Leg("long", hi, sl, hi + 2 * (hi - sl)), Leg("short", lo, ss, lo - 2 * (ss - lo))]
        out.append(Signal(int(idxs[i]), "F1", f"F1|pct={p}|atr={k}", legs, "stop_entry", valid_bars=15, max_hold=720))
    return out


def f2(m: Market, sig_k: int, mult: float, hold: int):
    c = pd.Series(m.c)
    ret = c.pct_change()
    sigma = ret.rolling(1440).std().shift(1)
    vr = pd.Series(m.v) / pd.Series(m.v).rolling(1440).mean().shift(1)
    cond = (ret.abs() > sig_k * sigma) & (vr > 5)
    out = []
    for i in np.flatnonzero(cond.fillna(False).to_numpy()):
        s = float(sigma.iloc[i]); move = m.c[i] - m.c[i - 1]
        if move > 0:        # spike up: sell a limit further up, stop 1.5 x the move above entry, target half the move back
            entry = m.c[i] * (1 + mult * s)
            leg = Leg("short", entry, entry + 1.5 * abs(move), m.c[i] - 0.5 * abs(move))
        else:
            entry = m.c[i] * (1 - mult * s)
            leg = Leg("long", entry, entry - 1.5 * abs(move), m.c[i] + 0.5 * abs(move))
        out.append(Signal(int(i), "F2", f"F2|sigma={sig_k}|m={mult}|hold={hold}", [leg], "limit", valid_bars=5, max_hold=hold))
    return out


def f3(m: Market, p: int, a: float, hold_h: int):
    f = m.funding_df
    rate = f["close"].astype(float)
    rank = rate.rolling(720).rank(pct=True)
    ends = (f["time"].to_numpy(np.int64) + 3600)
    out = []
    for i in np.flatnonzero(((rank >= 1 - p / 100) | (rank <= p / 100)).fillna(False).to_numpy()):
        idx = int(np.searchsorted(m.t, ends[i] - 60, side="right") - 1)
        if idx < 0 or m.t[idx] != ends[i] - 60:
            continue
        atr = m.atr_1h[idx]
        if not np.isfinite(atr) or atr <= 0:
            continue
        crowd_long = rank.iloc[i] >= 1 - p / 100 and rank.iloc[i] > 0.5
        px = m.c[idx]
        leg = Leg("short", px, px + a * atr, None) if crowd_long else Leg("long", px, px - a * atr, None)
        out.append(Signal(idx, "F3", f"F3|pct={p}|atr={a}|hold={hold_h}h", [leg], "limit", valid_bars=30, max_hold=hold_h * 60))
    return out


def f4(m: Market, lookback_h: int):
    bars = resample(m, "1h")
    starts = _epoch_s(bars.index)
    idxs = _idx_of_close(m, starts, 3600)
    sign = np.sign((bars["c"] / bars["c"].shift(lookback_h) - 1).to_numpy())
    out = []
    for i in range(lookback_h, len(bars) - 1):
        if sign[i] == 0 or np.isnan(sign[i]) or idxs[i] < 0 or m.t[idxs[i]] != starts[i] + 3540:
            continue
        j = i + 1
        while j < len(bars) and sign[j] == sign[i] and j - i < 24:
            j += 1
        atr = m.atr_1h[idxs[i]]
        if not np.isfinite(atr) or atr <= 0:
            continue
        px = m.c[idxs[i]]
        leg = Leg("long", px, px - 2 * atr, None) if sign[i] > 0 else Leg("short", px, px + 2 * atr, None)
        out.append(Signal(int(idxs[i]), "F4", f"F4|lookback={lookback_h}h", [leg], "limit", valid_bars=5, max_hold=min(1440, (j - i) * 60)))
    return out


def f5(m: Market, prior: int, hold: int):
    out = []
    day_starts = np.arange(m.t[0] - m.t[0] % 86400 + 86400, m.t[-1], 86400, dtype=np.int64)
    for ds in day_starts:
        for label, at in (("13:30", ds + 13 * 3600 + 1800), ("00:00", ds)):
            idx = int(np.searchsorted(m.t, at - 60, side="right") - 1)
            if idx < prior or m.t[idx] != at - 60:
                continue
            move = m.c[idx] - m.c[idx - prior]
            atr = m.atr_1h[idx]
            if move == 0 or not np.isfinite(atr) or atr <= 0:
                continue
            px = m.c[idx]
            leg = Leg("long", px, px - F5_STOP_ATR * atr, None) if move > 0 else Leg("short", px, px + F5_STOP_ATR * atr, None)
            out.append(Signal(idx, "F5", f"F5|prior={prior}m|hold={hold}m", [leg], "market", max_hold=hold, tag=label))
    out.sort(key=lambda s: s.idx)
    return out


def f6(m: Market):
    """Candle-direction PROXY for the order-flow control (no signed-trade history exists publicly): sign of the last 1-minute bar, held one bar, taker."""
    body = m.c - m.o
    for i in np.flatnonzero(body != 0):
        atr = m.atr_1h[i]
        if not np.isfinite(atr) or atr <= 0:
            continue
        px = m.c[i]
        yield Signal(int(i), "F6", "F6|proxy", [Leg("long", px, px - F6_STOP_ATR * atr, None) if body[i] > 0 else Leg("short", px, px + F6_STOP_ATR * atr, None)], "market", max_hold=1)


class _Lazy:
    """Re-iterable lazy signals (the F6 proxy would otherwise hold over a million objects)."""

    def __init__(self, fn, m):
        self.fn, self.m = fn, m

    def __iter__(self):
        return iter(self.fn(self.m))


NAMES = {
    "F1": lambda g: f"F1|pct={g[0]}|atr={g[1]}", "F2": lambda g: f"F2|sigma={g[0]}|m={g[1]}|hold={g[2]}", "F3": lambda g: f"F3|pct={g[0]}|atr={g[1]}|hold={g[2]}h",
    "F4": lambda g: f"F4|lookback={g[0]}h", "F5": lambda g: f"F5|prior={g[0]}m|hold={g[1]}m", "F6": lambda g: "F6|proxy",
}
FUNCS = {"F1": f1, "F2": f2, "F3": f3, "F4": f4, "F5": f5, "F6": lambda mm, _x: _Lazy(f6, mm)}


def variants(m: Market, family: str):
    """[(variant name, signals)] for the family's fixed grid (signals are materialised lists)."""
    return [(NAMES[family](g), FUNCS[family](m, *g)) for g in GRIDS[family]]
