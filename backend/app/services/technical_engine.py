"""Multi-horizon technical engine: direction, regime, support/resistance zones, entry scenarios, stops and targets.

Pure and deterministic. Input is real, completed daily OHLCV (oldest first); weekly and monthly candles are AGGREGATED from it (never approximated with
longer daily periods) and only COMPLETED periods are used. Indicator maths uses float internally (statistical transforms); every price that leaves this
module is a Decimal string quantised to the tick. A model never emits a level. Missing inputs make a component or a whole horizon unavailable with a
stated reason; they never default to bullish or bearish.

Horizons: swing = daily candles, positional = weekly candles, long_term = monthly candles.
Deliberately lean: direction comes from trend, structure and momentum only (the evidence that decides an entry). Volume, relative strength and volatility are used where
they act (breakout confirmation, context, stop sizing), not as extra score weights. Long-only: a bearish read means no entry. Decision support, not investment advice.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import Any, Sequence

import numpy as np
import pandas as pd

TICK = Decimal("0.05")
HORIZONS = ("swing", "positional", "long_term")
PRIMARY = {"swing": "D", "positional": "W", "long_term": "M"}
CONFIRM = {"swing": "W", "positional": "D", "long_term": "W"}
LABELS = {"D": "daily", "W": "weekly", "M": "monthly"}
MIN_BARS = {"D": 120, "W": 52, "M": 24}      # completed candles needed on the primary timeframe


@dataclass
class EngineConfig:
    """Every threshold is configurable; the defaults are starting hypotheses, not validated universal rules."""
    weights: dict[str, float] = field(default_factory=lambda: {"trend": 40, "structure": 30, "momentum": 30})
    breakout_buffer_atr: float = 0.2
    cluster_atr: float = 0.5                     # levels closer than this (in ATR) merge into one zone
    extension_atr: tuple[float, float, float] = (0.5, 1.0, 1.5)   # normal | acceptable | caution | avoid-chasing
    structural_buffer_atr: dict[str, float] = field(default_factory=lambda: {"swing": 0.25, "positional": 0.35, "long_term": 0.5})
    atr_stop_mult: dict[str, float] = field(default_factory=lambda: {"swing": 1.5, "positional": 2.0, "long_term": 3.0})
    max_risk_atr: dict[str, float] = field(default_factory=lambda: {"swing": 3.5, "positional": 4.5, "long_term": 6.0})
    min_rr_first_target: dict[str, float] = field(default_factory=lambda: {"swing": 1.3, "positional": 1.5, "long_term": 1.5})
    entry_zone_atr: float = 1.0                  # the buy band is at most this many DAILY ATRs wide, whatever the horizon (execution happens on the daily chart)
    entry_zone_max_pct: dict[str, float] = field(default_factory=lambda: {"swing": 1.2, "positional": 2.0, "long_term": 3.0})   # and never wider than this % of price
    pullback_reach_max_pct: dict[str, float] = field(default_factory=lambda: {"swing": 6.0, "positional": 10.0, "long_term": 15.0})   # a support further below price than this is not a practical pullback level
    pullback_reach_atr: float = 3.0              # a support further below price than this is not a pullback candidate
    min_rvol_breakout: float = 1.2
    adx_trending: float = 22.0
    direction_bands: tuple[int, int, int, int] = (80, 65, 45, 30)


# ───────────────────────────── helpers ─────────────────────────────
def _q(value: float, rounding: str = ROUND_HALF_UP) -> Decimal:
    return ((Decimal(str(round(float(value), 6))) / TICK).to_integral_value(rounding=rounding) * TICK).quantize(TICK)   # via string, never Decimal(float)


def _s(value: float, rounding: str = ROUND_HALF_UP) -> str:
    return str(_q(value, rounding))


def frame_from_bars(bars: Sequence[tuple[date, float, float, float, float, float]]) -> pd.DataFrame:
    """bars: (session date, open, high, low, close, volume), oldest first -> DataFrame indexed by date."""
    df = pd.DataFrame(bars, columns=["date", "open", "high", "low", "close", "volume"])
    df["date"] = pd.to_datetime(df["date"])
    return df.drop_duplicates("date").sort_values("date").set_index("date")


def resample(df: pd.DataFrame, tf: str, as_of: pd.Timestamp | None = None) -> pd.DataFrame:
    """Aggregate to weekly ('W', Friday-ended) or monthly ('M') candles and DROP the still-forming period (completed candles only).

    ``as_of`` is the date the analysis is made on (default: the last daily bar, which is conservative: its week/month counts as forming unless it ended)."""
    if tf == "D":
        return df
    agg = df.resample("W-FRI" if tf == "W" else "ME").agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna(subset=["close"])
    as_of = pd.Timestamp(as_of) if as_of is not None else df.index[-1]
    if len(agg) and as_of < agg.index[-1]:
        agg = agg.iloc[:-1]
    return agg


def _bench_on(index: pd.DatetimeIndex, bench_daily: pd.Series | None, tf: str, as_of: pd.Timestamp | None) -> pd.Series | None:
    if bench_daily is None or len(bench_daily) == 0:
        return None
    b = bench_daily.sort_index()
    if tf != "D":
        frame = pd.DataFrame({"open": b, "high": b, "low": b, "close": b, "volume": 0.0})
        b = resample(frame, tf, as_of)["close"]
    return b.reindex(index, method="ffill").dropna()


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    pc = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()   # Wilder smoothing


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    up, dn = d.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean(), (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def adx(df: pd.DataFrame, n: int = 14) -> pd.Series:
    up, dn = df["high"].diff(), -df["low"].diff()
    plus = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=df.index)
    minus = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=df.index)
    a = atr(df, n)
    pdi = 100 * plus.ewm(alpha=1 / n, adjust=False, min_periods=n).mean() / a
    mdi = 100 * minus.ewm(alpha=1 / n, adjust=False, min_periods=n).mean() / a
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return dx.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def macd_hist(close: pd.Series) -> pd.Series:
    line = ema(close, 12) - ema(close, 26)
    return line - line.ewm(span=9, adjust=False, min_periods=9).mean()


def swing_points(df: pd.DataFrame, k: int) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
    """Fractal swing highs/lows confirmed by k candles on each side (so the last k candles can never be a swing: no look-ahead)."""
    hi, lo = df["high"].to_numpy(), df["low"].to_numpy()
    highs, lows = [], []
    for i in range(k, len(df) - k):
        if hi[i] == hi[i - k:i + k + 1].max() and hi[i] > hi[i - 1]:
            highs.append((i, float(hi[i])))
        if lo[i] == lo[i - k:i + k + 1].min() and lo[i] < lo[i - 1]:
            lows.append((i, float(lo[i])))
    return highs, lows


def classic_pivots(h: float, l: float, c: float) -> dict[str, float]:
    p = (h + l + c) / 3
    return {"S2": p - (h - l), "S1": 2 * p - h, "P": p, "R1": 2 * p - l, "R2": p + (h - l)}


# ───────────────────────────── per-timeframe indicator pack ─────────────────────────────
def indicators(df: pd.DataFrame) -> dict[str, Any]:
    c = df["close"]
    a14 = atr(df, 14)
    last = lambda x: (None if x.dropna().empty else float(x.dropna().iloc[-1]))  # noqa: E731
    nat = (a14 / c * 100)
    nat_hist = nat.dropna().iloc[-250:]
    pct = None if nat_hist.empty else float((nat_hist <= nat_hist.iloc[-1]).mean())
    vol_ma = df["volume"].rolling(20).mean()
    rvol = None if not vol_ma.iloc[-1] or np.isnan(vol_ma.iloc[-1]) or vol_ma.iloc[-1] == 0 else float(df["volume"].iloc[-1] / vol_ma.iloc[-1])
    e20, e50, e200 = ema(c, 20), ema(c, 50), ema(c, 200)
    hist = macd_hist(c)
    return {
        "close": float(c.iloc[-1]), "atr14": last(a14), "natr": last(nat), "natr_percentile": pct,
        "ema20": last(e20), "ema50": last(e50), "ema200": last(e200),
        "ema50_slope": None if e50.dropna().shape[0] < 11 else float((e50.iloc[-1] / e50.iloc[-11] - 1) * 100),
        "rsi14": last(rsi(c)), "adx14": last(adx(df)), "macd_hist": last(hist), "macd_hist_prev": None if hist.dropna().shape[0] < 2 else float(hist.dropna().iloc[-2]),
        "rvol": rvol, "low52": float(df["low"].iloc[-252:].min()), "high52": float(df["high"].iloc[-252:].max()),
    }


# ───────────────────────────── support / resistance zones ─────────────────────────────
def build_zones(df: pd.DataFrame, ind: dict[str, Any], daily: pd.DataFrame, weekly: pd.DataFrame | None, monthly: pd.DataFrame | None, k: int, cfg: EngineConfig) -> list[dict[str, Any]]:
    a = ind["atr14"]
    if not a:
        return []
    cand: list[tuple[float, str, str]] = []     # (price, family, label)
    highs, lows = swing_points(df, k)
    for _, v in highs[-12:]:
        cand.append((v, "swing", "swing high"))
    for _, v in lows[-12:]:
        cand.append((v, "swing", "swing low"))
    for name in ("ema20", "ema50", "ema200"):
        if ind.get(name):
            cand.append((ind[name], "ema", name.upper().replace("EMA", "EMA ")))
    for label, frame in (("daily", daily), ("weekly", weekly), ("monthly", monthly)):
        if frame is None or len(frame) < 2:
            continue
        prev = frame.iloc[-1]                  # the frame passed in already excludes the forming period
        for lvl, v in classic_pivots(float(prev["high"]), float(prev["low"]), float(prev["close"])).items():
            cand.append((v, "pivot", f"{label} pivot {lvl}"))
        cand.append((float(prev["high"]), "period", f"previous {label} high"))
        cand.append((float(prev["low"]), "period", f"previous {label} low"))
    cand.append((ind["high52"], "period", "52-week high"))
    cand.append((ind["low52"], "period", "52-week low"))
    cand = sorted((c for c in cand if c[0] and not np.isnan(c[0])), key=lambda t: t[0])
    zones: list[list[tuple[float, str, str]]] = []
    for item in cand:
        if zones and item[0] - zones[-1][-1][0] <= cfg.cluster_atr * a and item[0] - zones[-1][0][0] <= 1.5 * a:   # chains are capped so one zone never spans a huge range
            zones[-1].append(item)
        else:
            zones.append([item])
    price = ind["close"]
    out = []
    for z in zones:
        prices = [p for p, _, _ in z]
        families = {f for _, f, _ in z}
        touches = sum(1 for _, f, _ in z if f == "swing")
        strength = len(families) + min(touches, 3) * 0.5
        out.append({"low": min(prices), "high": max(prices), "mid": float(np.mean(prices)), "strength": round(strength, 1), "families": sorted(families),
                    "labels": sorted({lab for _, _, lab in z})[:6], "kind": "support" if np.mean(prices) < price else "resistance"})
    return out


# ───────────────────────────── direction / regime ─────────────────────────────
def _clip(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def components(df: pd.DataFrame, ind: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The three scored components. ADX sets trend STRENGTH, never direction; RSI above 70 is not an automatic sell (it only damps the RSI term)."""
    out: dict[str, dict[str, Any]] = {}
    c = ind["close"]

    def put(name: str, score: float | None, why: str) -> None:
        out[name] = {"score": None if score is None else round(max(0.0, min(100.0, score)), 1), "why": why}

    votes = [c > ind["ema20"] if ind["ema20"] else None, c > ind["ema50"] if ind["ema50"] else None, (ind["ema20"] > ind["ema50"]) if ind["ema20"] and ind["ema50"] else None,
             (ind["ema50_slope"] > 0) if ind["ema50_slope"] is not None else None, (c > ind["ema200"]) if ind["ema200"] else None,
             (ind["ema50"] > ind["ema200"]) if ind["ema50"] and ind["ema200"] else None]
    known = [v for v in votes if v is not None]
    if len(known) >= 3 and ind["adx14"] is not None:
        direction = (sum(1 if v else -1 for v in known)) / len(known)
        strength = _clip(ind["adx14"] / 35, 0.35, 1.0)
        put("trend", 50 + 50 * direction * strength, f"{sum(known)} of {len(known)} trend checks bullish (price/EMA20/EMA50/EMA200 alignment, EMA50 slope); ADX {ind['adx14']:.0f} sets the strength.")
    else:
        put("trend", None, "Not enough history for EMA alignment and ADX.")
    highs, lows = swing_points(df, 3 if len(df) > 150 else 2)
    if len(highs) >= 2 and len(lows) >= 2:
        hh, hl = highs[-1][1] > highs[-2][1], lows[-1][1] > lows[-2][1]
        s_ = 1.0 if hh and hl else -1.0 if (not hh and not hl) else 0.0
        put("structure", 50 + 50 * s_, "Higher highs and higher lows." if s_ > 0 else "Lower highs and lower lows." if s_ < 0 else "Mixed swing structure (consolidation).")
    else:
        put("structure", None, "Too few confirmed swing points.")
    if ind["rsi14"] is not None and ind["macd_hist"] is not None:
        r = _clip((ind["rsi14"] - 50) / 20) * (0.5 if ind["rsi14"] > 78 else 1.0)
        m = 0.5 * np.sign(ind["macd_hist"]) + (0.5 * np.sign(ind["macd_hist"] - ind["macd_hist_prev"]) if ind["macd_hist_prev"] is not None else 0)
        put("momentum", 50 + 50 * (0.5 * r + 0.5 * m), f"RSI {ind['rsi14']:.0f}, MACD histogram {'positive' if ind['macd_hist'] > 0 else 'negative'} and {'rising' if (ind['macd_hist_prev'] is not None and ind['macd_hist'] > ind['macd_hist_prev']) else 'falling'}.")
    else:
        put("momentum", None, "Not enough history for RSI/MACD.")
    return out


def relative_strength_note(df: pd.DataFrame, bench: pd.Series | None, windows: tuple[int, int]) -> dict[str, Any]:
    """Context only (not scored): return versus the benchmark. Unavailable, never neutral, when no benchmark history is stored."""
    if bench is None or len(bench) <= windows[1] or len(df) <= windows[1]:
        return {"available": False, "note": "No benchmark index history is stored, so relative strength is not shown."}
    diffs = [float(df["close"].iloc[-1] / df["close"].iloc[-1 - w] - bench.iloc[-1] / bench.iloc[-1 - w]) for w in windows]
    return {"available": True, "vs_benchmark_pct_points": [round(d * 100, 1) for d in diffs], "windows": list(windows),
            "note": f"Return versus the benchmark over {windows[0]} and {windows[1]} candles: {diffs[0] * 100:+.1f} and {diffs[1] * 100:+.1f} percentage points."}


def direction_of(score: float | None, bands: tuple[int, int, int, int]) -> str | None:
    if score is None:
        return None
    s, b, n, br = bands
    return "STRONG_BULLISH" if score >= s else "BULLISH" if score >= b else "NEUTRAL" if score >= n else "BEARISH" if score >= br else "STRONG_BEARISH"


def regime_of(ind: dict[str, Any], comps: dict[str, dict[str, Any]]) -> str:
    adx_v, trend, pct = ind["adx14"], comps["trend"]["score"], ind["natr_percentile"]
    if adx_v is None or trend is None:
        return "UNKNOWN"
    if pct is not None and pct <= 0.2 and adx_v < 20:
        return "VOLATILITY_CONTRACTION"
    if adx_v < 20:
        return "SIDEWAYS"
    return ("STRONG_UPTREND" if trend >= 70 and adx_v >= 28 else "WEAK_UPTREND") if trend >= 58 else "DOWNTREND" if trend <= 42 else "SIDEWAYS"


# ───────────────────────────── entry scenarios, stops, targets ─────────────────────────────
def _extension_label(x: float, cfg: EngineConfig) -> str:
    n, ok, caution = cfg.extension_atr
    return "NORMAL" if x < n else "ACCEPTABLE_WITH_CONFIRMATION" if x < ok else "CAUTION" if x < caution else "AVOID_CHASING"


def entry_scenarios(horizon: str, ind: dict[str, Any], zones: list[dict[str, Any]], regime: str, cfg: EngineConfig, atr_exec: float | None = None) -> dict[str, dict[str, Any]]:
    c, a = ind["close"], ind["atr14"]
    ae = atr_exec or a                                    # daily ATR: the buy band is sized on the chart where the order is placed
    width = min(cfg.entry_zone_atr * ae, cfg.entry_zone_max_pct[horizon] / 100 * c)
    bullish_ok = regime != "DOWNTREND"
    confirm_momentum = (ind["macd_hist"] or 0) > 0 and (ind["rsi14"] or 0) > 48
    supports = sorted((z for z in zones if z["kind"] == "support"), key=lambda z: -z["high"])
    resist = sorted((z for z in zones if z["kind"] == "resistance"), key=lambda z: z["low"])
    out: dict[str, dict[str, Any]] = {}

    # A: pullback to the nearest MEANINGFUL support (multi-factor zone), not merely any touched line
    sup = next((z for z in supports if (z["strength"] >= 2 or "ema" in z["families"]) and c - z["high"] <= cfg.pullback_reach_atr * a and (c - z["high"]) / c * 100 <= cfg.pullback_reach_max_pct[horizon]), None)
    if sup is None or not bullish_ok:
        out["PULLBACK_ENTRY"] = {"status": "INVALID", "reason": "Downtrend regime; dip-buying is avoided." if not bullish_ok else f"No multi-factor support zone within {cfg.pullback_reach_atr:g} ATR and {cfg.pullback_reach_max_pct[horizon]:g}% below price."}
    else:
        hi = sup["high"] + 0.25 * ae                       # anchored at the top of the support zone, where buyers step in on a pullback
        lo = max(sup["low"], hi - width)                    # the band stays narrow even when the support cluster is wide
        ext = max(0.0, (c - hi)) / a
        inside = lo - 0.25 * ae <= c <= hi
        status = "ENTER_NOW" if inside and confirm_momentum else "WAIT"
        out["PULLBACK_ENTRY"] = {"status": status, "zone": [lo, hi], "reference": sup["mid"], "extension_atr": round(ext, 2), "extension": _extension_label(ext, cfg), "support_zone": sup,
                                 "reason": (f"Price is inside the support zone ({', '.join(sup['labels'][:3])})." if inside else f"Price is {ext:.1f} ATR above the support zone ({', '.join(sup['labels'][:3])}); wait for a pullback.") + ("" if confirm_momentum or not inside else " Momentum is not confirming yet.")}

    # B: breakout through the nearest resistance, with volume and momentum confirmation and an ATR extension cap
    broken = next((z for z in sorted((z for z in zones if z["strength"] >= 2 and z["high"] < c), key=lambda z: -z["high"]) if c - z["high"] <= 2 * a), None)
    ahead = next((z for z in resist if z["strength"] >= 2 and z["low"] - c <= 2.5 * a), None)
    target_zone = broken or ahead
    if target_zone is None:
        out["BREAKOUT_ENTRY"] = {"status": "INVALID", "reason": "No significant resistance within reach to break."}
    else:
        trigger = target_zone["high"] + cfg.breakout_buffer_atr * a
        ext = (c - trigger) / a if c > trigger else 0.0
        vol_ok = (ind["rvol"] or 0) >= cfg.min_rvol_breakout
        if c > trigger:
            confirmed = vol_ok and confirm_momentum
            chase = ext > cfg.extension_atr[2]
            status = "INVALID" if chase else "ENTER_NOW" if confirmed and ext <= cfg.extension_atr[1] else "WAIT"
            reason = (f"Closed {ext:.1f} ATR above the breakout trigger ({_s(trigger)}). " + ("Too extended; do not chase." if chase else "Volume and momentum confirm." if confirmed else "Volume or momentum has not confirmed; wait for a retest."))
        else:
            status, reason = "WAIT", f"Needs a close above {_s(trigger)} (resistance {', '.join(target_zone['labels'][:2])} + {cfg.breakout_buffer_atr:g} ATR) with volume and momentum."
        out["BREAKOUT_ENTRY"] = {"status": status, "trigger": trigger, "zone": [trigger, trigger + min(0.5 * ae, width)], "reference": trigger, "extension_atr": round(ext, 2), "extension": _extension_label(ext, cfg),
                                 "resistance_zone": target_zone, "reason": reason}

    # C: momentum continuation: strong aligned trend, not stretched from its reference EMA
    e20 = ind["ema20"]
    if e20 and ind["adx14"] and ind["adx14"] >= cfg.adx_trending and regime in ("STRONG_UPTREND", "WEAK_UPTREND") and c > e20 and confirm_momentum:
        ext = (c - e20) / a
        out["MOMENTUM_ENTRY"] = {"status": "ENTER_NOW" if ext <= cfg.extension_atr[1] else "INVALID" if ext > cfg.extension_atr[2] else "WAIT", "zone": [max(c - 0.5 * ae, c - width), c], "reference": e20,
                                 "extension_atr": round(ext, 2), "extension": _extension_label(ext, cfg),
                                 "reason": f"Trend is up with ADX {ind['adx14']:.0f}; price is {ext:.1f} ATR above the 20-EMA ({_extension_label(ext, cfg).replace('_', ' ').lower()})."}
    else:
        out["MOMENTUM_ENTRY"] = {"status": "INVALID", "reason": "Needs an up-trend regime with ADX above the trending threshold, price above the 20-EMA and confirming momentum."}
    return out


def choose_setup(scen: dict[str, dict[str, Any]], regime: str) -> tuple[str, str]:
    prefs = {"STRONG_UPTREND": ("MOMENTUM_ENTRY", "PULLBACK_ENTRY", "BREAKOUT_ENTRY"), "WEAK_UPTREND": ("PULLBACK_ENTRY", "MOMENTUM_ENTRY", "BREAKOUT_ENTRY"),
             "SIDEWAYS": ("PULLBACK_ENTRY", "BREAKOUT_ENTRY", "MOMENTUM_ENTRY"), "VOLATILITY_CONTRACTION": ("BREAKOUT_ENTRY", "PULLBACK_ENTRY", "MOMENTUM_ENTRY")}.get(regime, ())
    for name in prefs:
        if scen[name]["status"] == "ENTER_NOW":
            return name, "ENTER_NOW"
    for name in prefs:
        if scen[name]["status"] == "WAIT":
            return name, "WAIT_FOR_ENTRY"
    return "NO_VALID_SETUP", "NO_VALID_SETUP"


def stop_and_targets(horizon: str, setup: str, scen: dict[str, Any], status: str, ind: dict[str, Any], zones: list[dict[str, Any]], cfg: EngineConfig, swing_lows: list[float]) -> dict[str, Any] | None:
    if setup == "NO_VALID_SETUP":
        return None
    a, c = ind["atr14"], ind["close"]
    zone = scen["zone"]
    entry = c if status == "ENTER_NOW" else (zone[0] + zone[1]) / 2 if setup == "PULLBACK_ENTRY" else zone[0]
    entry = min(max(entry, zone[0]), max(zone[1], c)) if status == "ENTER_NOW" else entry
    # structural invalidation = the zone / swing low that, if lost, breaks the thesis
    if setup == "PULLBACK_ENTRY":
        structure = scen["support_zone"]["low"]
    elif setup == "BREAKOUT_ENTRY":
        structure = scen["resistance_zone"]["low"]
    else:
        below = [z for z in zones if z["kind"] == "support" and z["high"] < entry]
        structure = max((z["low"] for z in below), default=min(swing_lows[-1:] or [entry - 2 * a]))
    structural = structure - cfg.structural_buffer_atr[horizon] * a
    atr_only = entry - cfg.atr_stop_mult[horizon] * a
    limit = cfg.max_risk_atr[horizon] * a
    if 0 < entry - structural <= limit and entry - structural >= 0.4 * a:
        stop, method, why = structural, "STRUCTURAL", f"Just beyond structural support ({_s(structure)}) with a {cfg.structural_buffer_atr[horizon]:g} ATR buffer."
    elif entry - atr_only > 0:
        stop, method, why = atr_only, "ATR", f"Structural stop would be {(entry - structural) / a:.1f} ATR away (limit {cfg.max_risk_atr[horizon]:g}) or too tight; used {cfg.atr_stop_mult[horizon]:g} ATR below entry."
    else:
        return None
    risk = entry - stop
    # targets: nearest meaningful resistance zones first, then risk-reward multiples (always labelled as such)
    cands: list[tuple[float, str]] = []
    for z in sorted((z for z in zones if z["kind"] == "resistance" and z["low"] > entry + 0.3 * risk), key=lambda z: z["low"]):
        cands.append((z["low"] - 0.1 * a, f"Resistance zone {_s(z['low'])}–{_s(z['high'])} ({', '.join(z['labels'][:2])})"))
    targets: list[dict[str, Any]] = []
    for price, why_t in sorted(cands, key=lambda t: t[0]):
        if price > entry + 0.5 * risk and (not targets or price - targets[-1]["_p"] >= 0.5 * risk):
            targets.append({"_p": price, "basis": why_t})
        if len(targets) == 3:
            break
    for m in (1.5, 2.5, 4.0):                    # only fills missing slots, always labelled risk-reward based
        if len(targets) >= 3:
            break
        price = entry + m * risk
        if not targets or price > targets[-1]["_p"] + 0.25 * risk:
            targets.append({"_p": price, "basis": f"Risk-reward based ({m:g}R); market structure offers no nearer level"})
    for i, t in enumerate(targets, 1):
        t.update(label=f"T{i}", price=_s(t["_p"]), risk_reward=round((t["_p"] - entry) / risk, 2))
    first_rr = targets[0]["risk_reward"] if targets else 0
    return {"entry": entry, "stop": stop, "method": method, "why": why, "risk": risk, "structure": structure, "targets": [{k: v for k, v in t.items() if k != "_p"} for t in targets],
            "enough_upside": first_rr >= cfg.min_rr_first_target[horizon], "rr_first": first_rr}


# ───────────────────────────── per-horizon assembly ─────────────────────────────
def analyze_horizon(horizon: str, daily: pd.DataFrame, bench_daily: pd.Series | None, cfg: EngineConfig, as_of: pd.Timestamp | None = None) -> dict[str, Any]:
    tf = PRIMARY[horizon]
    weekly, monthly = resample(daily, "W", as_of), resample(daily, "M", as_of)
    df = {"D": daily, "W": weekly, "M": monthly}[tf]
    if len(df) < MIN_BARS[tf]:
        return {"status": "unavailable", "reason": f"Only {len(df)} completed {LABELS[tf]} candles are stored; at least {MIN_BARS[tf]} are needed.", "primary_timeframe": LABELS[tf]}
    ind = indicators(df)
    if not ind["atr14"]:
        return {"status": "unavailable", "reason": "ATR could not be computed.", "primary_timeframe": LABELS[tf]}
    k = 3 if tf == "D" else 2
    zones = build_zones(df, ind, daily, weekly if len(weekly) > 1 else None, monthly if len(monthly) > 1 else None, k, cfg)
    windows = {"D": (20, 50), "W": (13, 26), "M": (6, 12)}[tf]
    bench = _bench_on(df.index, bench_daily, tf, as_of)
    comps = components(df, ind)
    rs_note = relative_strength_note(df, bench, windows)
    scored = {n: v for n, v in comps.items() if v["score"] is not None}
    wsum = sum(cfg.weights[n] for n in scored)
    covered = wsum / sum(cfg.weights.values())
    if "trend" not in scored or covered < 0.6:
        return {"status": "unavailable", "reason": "Too few technical components could be scored to give a direction.", "components": comps, "primary_timeframe": LABELS[tf]}
    score = sum(cfg.weights[n] * v["score"] for n, v in scored.items()) / wsum
    direction = direction_of(score, cfg.direction_bands)
    regime = regime_of(ind, comps)
    atr_d = float(atr(daily, 14).iloc[-1]) if len(daily) > 20 else None
    scen = entry_scenarios(horizon, ind, zones, regime, cfg, atr_d)
    setup, status = choose_setup(scen, regime) if direction in ("STRONG_BULLISH", "BULLISH", "NEUTRAL") else ("NO_VALID_SETUP", "NO_VALID_SETUP")
    if direction == "NEUTRAL" and status == "ENTER_NOW":
        status = "WAIT_FOR_ENTRY"          # a neutral reading never says "enter now"
    lows = [v for _, v in swing_points(df, k)[1]]
    plan = stop_and_targets(horizon, setup, scen.get(setup, {}), status, ind, zones, cfg, lows) if setup != "NO_VALID_SETUP" else None
    if plan is not None and status == "ENTER_NOW" and not plan["enough_upside"]:
        status = "WAIT_FOR_ENTRY"
    if setup != "NO_VALID_SETUP" and plan is None:
        setup, status = "NO_VALID_SETUP", "NO_VALID_SETUP"
    a = ind["atr14"]
    reasons = [v["why"] for n, v in sorted(scored.items(), key=lambda kv: -cfg.weights[kv[0]])]
    risks = [v["why"] for v in scored.values() if v["score"] < 40] + ([f"{n.replace('_', ' ').title()} is not scored: {v['why']}" for n, v in comps.items() if v["score"] is None])
    if plan is not None and not plan["enough_upside"]:
        risks.append(f"First target offers {plan['rr_first']:.1f}R, below the {cfg.min_rr_first_target[horizon]:g}R this horizon requires.")
    if ind["rvol"] is not None and ind["rvol"] < 0.8:
        risks.append(f"Relative volume is only {ind['rvol']:.1f}x of its 20-bar average.")
    supports = sorted((z for z in zones if z["kind"] == "support"), key=lambda z: -z["high"])[:4]
    resist = sorted((z for z in zones if z["kind"] == "resistance"), key=lambda z: z["low"])[:4]
    zj = lambda z: {"low": _s(z["low"], ROUND_DOWN), "high": _s(z["high"]), "strength": z["strength"], "labels": z["labels"]}  # noqa: E731
    sel = scen.get(setup, {})
    pv = {name: {k2: _s(v) for k2, v in classic_pivots(float(fr.iloc[-1]["high"]), float(fr.iloc[-1]["low"]), float(fr.iloc[-1]["close"])).items()}
          for name, fr in (("daily", daily), ("weekly", weekly), ("monthly", monthly)) if len(fr) > 1}
    return {
        "status": "available", "primary_timeframe": LABELS[tf], "confirmation_timeframe": LABELS[CONFIRM[horizon]], "last_completed_candle": df.index[-1].date().isoformat(),
        "direction": direction, "technical_score": round(score, 1), "score_coverage": round(covered, 2), "market_regime": regime, "components": comps,
        "selected_setup": setup, "entry_status": status, "current_price": _s(ind["close"]),
        "scenarios": {n: _scenario_out(sc) for n, sc in scen.items()},
        "entry": None if plan is None else {"low": _s(sel["zone"][0], ROUND_DOWN), "high": _s(sel["zone"][1]), "planned_price": _s(plan["entry"]), "breakout_trigger": _s(sel["trigger"]) if "trigger" in sel else None,
                                           "extension_atr": sel.get("extension_atr"), "extension": sel.get("extension")},
        "stop_loss": None if plan is None else {"price": _s(plan["stop"], ROUND_DOWN), "percent": round(plan["risk"] / plan["entry"] * 100, 2), "atr_units": round(plan["risk"] / a, 2), "method": plan["method"],
                                               "reason": plan["why"], "structural_invalidation": _s(plan["structure"], ROUND_DOWN)},
        "targets": [] if plan is None else plan["targets"],
        "risk_reward": None if plan is None else {t["label"]: t["risk_reward"] for t in plan["targets"]},
        "enough_upside": None if plan is None else plan["enough_upside"],
        "atr": {"atr14": round(a, 4), "natr": round(ind["natr"], 2)},
        "indicators": {k2: (None if ind[k2] is None else round(ind[k2], 3)) for k2 in ("ema20", "ema50", "ema200", "rsi14", "adx14", "macd_hist", "rvol", "natr_percentile")},
        "support_levels": [zj(z) for z in supports], "resistance_levels": [zj(z) for z in resist], "pivots": pv, "relative_strength": rs_note,
        "reasons": reasons, "risks": risks,
        "explanation": explain(horizon, direction, regime, setup, status, ind, plan, scen.get(setup, {}), comps, rs_note),
    }


def _scenario_out(sc: dict[str, Any]) -> dict[str, Any]:
    out = {"status": sc["status"], "reason": sc["reason"]}
    if "zone" in sc:
        out["zone"] = [_s(x) for x in sc["zone"]]
    if "trigger" in sc:
        out["trigger"] = _s(sc["trigger"])
    if "extension_atr" in sc:
        out["extension_atr"], out["extension"] = sc["extension_atr"], sc["extension"]
    return out


def explain(horizon: str, direction: str, regime: str, setup: str, status: str, ind: dict[str, Any], plan: dict[str, Any] | None, sel: dict[str, Any], comps: dict[str, Any], rs: dict[str, Any]) -> str:
    """Plain-language reading assembled ONLY from the calculated values above (no generative text)."""
    tf = LABELS[PRIMARY[horizon]]
    parts = [f"{horizon.replace('_', '-').title()} view on {tf} candles is {direction.replace('_', ' ').lower()} in a {regime.replace('_', ' ').lower()} regime. {comps['trend']['why']}"]
    parts.append(rs["note"])
    if setup == "NO_VALID_SETUP":
        parts.append("No valid entry setup exists right now, so no entry level is offered.")
    else:
        parts.append(f"Selected setup: {setup.replace('_', ' ').lower()}. {sel.get('reason', '')}")
        if plan:
            parts.append(f"Stop {_s(plan['stop'], ROUND_DOWN)} ({plan['method'].lower()}, {plan['risk'] / ind['atr14']:.1f} ATR). First target {plan['targets'][0]['price']} gives {plan['rr_first']:.1f}R" + ("." if plan["enough_upside"] else "; that is less upside than this horizon requires, so wait."))
    return " ".join(parts)


def analyze(daily: pd.DataFrame, bench_daily: pd.Series | None = None, cfg: EngineConfig | None = None, as_of: pd.Timestamp | None = None) -> dict[str, Any]:
    """Full multi-horizon read of one stock from completed daily candles. Never raises for thin data: a horizon is simply unavailable with a reason."""
    cfg = cfg or EngineConfig()
    if daily is None or len(daily) == 0:
        return {"status": "unavailable", "reason": "No daily candles are stored.", "horizons": {}}
    out = {h: analyze_horizon(h, daily, bench_daily, cfg, as_of) for h in HORIZONS}
    return {"status": "available" if any(v["status"] == "available" for v in out.values()) else "unavailable", "last_daily_candle": daily.index[-1].date().isoformat(),
            "bars": len(daily), "horizons": out, "config": {"breakout_buffer_atr": cfg.breakout_buffer_atr, "cluster_atr": cfg.cluster_atr, "extension_atr": list(cfg.extension_atr),
                                                           "weights": cfg.weights},
            "label": "Decision support, not investment advice. Levels are computed from completed candles and are not orders."}
