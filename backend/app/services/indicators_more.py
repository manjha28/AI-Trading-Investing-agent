"""Indicator library 1.2.0: volume, structure and levels, candlestick patterns, rolling statistics.

Same contract as ``indicators_extra``: registered into the shared REGISTRY, causal (bar t uses bars <= t only), NaN while
warming up, session-aware where it matters (IST date for NSE). Confirmation-based indicators (swing/ZigZag/fractal style)
report a pivot only once enough later bars have confirmed it, so nothing repaints and nothing looks ahead.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view

from app.services.indicators_extra import _percent_rank
from app.services.intraday_indicators import (
    L, LEN, NAN, P, _div, _hlc, _roll_apply, ema, register, shift, sma, src, stdev,
)
from app.services.timeframes import session_keys


def _session_slices(df: pd.DataFrame, ctx) -> list[tuple[int, int]]:
    keys = session_keys(df["time"], ctx.profile).to_numpy()
    out, start = [], 0
    for i in range(1, len(df) + 1):
        if i == len(df) or keys[i] != keys[start]:
            out.append((start, i))
            start = i
    return out


# ───────────────────────── volume ─────────────────────────
@register("klinger", "Klinger Volume Oscillator", "volume", {"fast": L(34), "slow": L(55), "signal": L(13)}, ("kvo", "signal"),
          "EMA(VF, fast) − EMA(VF, slow); VF = volume·|2·(dm/cm − 1)|·trend·100; trend = sign(hlc3 − hlc3₋₁); signal = EMA(KVO)",
          lambda p: p["slow"] + p["signal"])
def _klinger(df, ctx, fast, slow, signal):
    h, l, c = _hlc(df)
    v = df["volume"].to_numpy(float)
    hlc = (h + l + c) / 3
    dm = h - l
    n = len(c)
    trend = np.full(n, NAN)
    trend[1:] = np.where(hlc[1:] > hlc[:-1], 1.0, -1.0)
    cm = np.full(n, NAN)
    if n:
        cm[0] = dm[0]
    for i in range(1, n):
        cm[i] = cm[i - 1] + dm[i] if trend[i] == trend[i - 1] or math.isnan(trend[i - 1]) else dm[i - 1] + dm[i]
    vf = v * np.abs(2 * (_div(dm, cm) - 1)) * trend * 100
    kvo = ema(vf, fast) - ema(vf, slow)
    return {"kvo": kvo, "signal": ema(kvo, signal)}


@register("force_index", "Elder Force Index", "volume", {"length": L(13)}, ("value",), "EMA((close − close₋₁)·volume, n)", lambda p: p["length"] + 1)
def _force(df, ctx, length):
    c, v = df["close"].to_numpy(float), df["volume"].to_numpy(float)
    return {"value": ema((c - shift(c, 1)) * v, length)}


@register("eom", "Ease of Movement", "volume", {"length": L(14), "divisor": P(10000, 1, 1e9, "float")}, ("value",),
          "SMA(((hl2 − hl2₋₁)·(high − low)) / (volume/divisor), n)", lambda p: p["length"] + 1)
def _eom(df, ctx, length, divisor):
    h, l, _ = _hlc(df)
    v = df["volume"].to_numpy(float)
    hl2 = (h + l) / 2
    emv = _div((hl2 - shift(hl2, 1)) * (h - l), v / divisor)
    return {"value": sma(emv, length)}


@register("pvt", "Price Volume Trend", "volume", {}, ("value",), "Σ volume·(close/close₋₁ − 1)", lambda p: 2)
def _pvt(df, ctx):
    c, v = df["close"].to_numpy(float), df["volume"].to_numpy(float)
    step = v * (_div(c, shift(c, 1)) - 1)
    out = np.cumsum(np.nan_to_num(step))
    out[0] = NAN
    return {"value": out}


def _volume_index(df: pd.DataFrame, when_volume_up: bool) -> np.ndarray:
    c, v = df["close"].to_numpy(float), df["volume"].to_numpy(float)
    out = np.full(len(c), NAN)
    if not len(c):
        return out
    out[0] = 1000.0
    for i in range(1, len(c)):
        moved = (v[i] > v[i - 1]) if when_volume_up else (v[i] < v[i - 1])
        out[i] = out[i - 1] * (1 + (c[i] - c[i - 1]) / c[i - 1]) if moved and c[i - 1] else out[i - 1]
    return out


@register("pvi", "Positive Volume Index", "volume", {}, ("value",), "starts at 1000; changes by the % price move only on bars where volume rose", lambda p: 1)
def _pvi(df, ctx):
    return {"value": _volume_index(df, True)}


@register("nvi", "Negative Volume Index", "volume", {}, ("value",), "starts at 1000; changes by the % price move only on bars where volume fell", lambda p: 1)
def _nvi(df, ctx):
    return {"value": _volume_index(df, False)}


@register("chaikin_osc", "Chaikin Oscillator", "volume", {"fast": L(3), "slow": L(10)}, ("value",),
          "EMA(AD, fast) − EMA(AD, slow); AD = Σ ((close−low)−(high−close))/(high−low)·volume", lambda p: p["slow"])
def _chaikin_osc(df, ctx, fast, slow):
    h, l, c = _hlc(df)
    v = df["volume"].to_numpy(float)
    ad = np.cumsum(np.nan_to_num(_div((c - l) - (h - c), h - l) * v))
    return {"value": ema(ad, fast) - ema(ad, slow)}


@register("williams_ad", "Williams Accumulation/Distribution", "volume", {}, ("value",),
          "Σ: up close → close − min(low, close₋₁); down close → close − max(high, close₋₁); unchanged → 0", lambda p: 2)
def _williams_ad(df, ctx):
    h, l, c = _hlc(df)
    n = len(c)
    out = np.full(n, NAN)
    if n:
        out[0] = 0.0
    for i in range(1, n):
        if c[i] > c[i - 1]:
            step = c[i] - min(l[i], c[i - 1])
        elif c[i] < c[i - 1]:
            step = c[i] - max(h[i], c[i - 1])
        else:
            step = 0.0
        out[i] = out[i - 1] + step
    out[0] = NAN
    return {"value": out}


@register("vwap_bands", "VWAP Standard-Deviation Bands", "volume", {"multiplier": P(1.0, 0.1, 10, "float")},
          ("vwap", "upper1", "lower1", "upper2", "lower2"), "session VWAP ± k·σ_v and ± 2k·σ_v; σ_v² = Σ(v·tp²)/Σv − VWAP² (volume-weighted), reset each session",
          lambda p: 1)
def _vwap_bands(df, ctx, multiplier):
    tp, v = src(df, "hlc3"), df["volume"].to_numpy(float)
    n = len(tp)
    out = {k: np.full(n, NAN) for k in ("vwap", "upper1", "lower1", "upper2", "lower2")}
    for a, b in _session_slices(df, ctx):
        cv = np.cumsum(v[a:b])
        vwap = _div(np.cumsum(tp[a:b] * v[a:b]), cv)
        sigma = np.sqrt(np.maximum(_div(np.cumsum(tp[a:b] ** 2 * v[a:b]), cv) - vwap ** 2, 0.0))
        out["vwap"][a:b] = vwap
        out["upper1"][a:b], out["lower1"][a:b] = vwap + multiplier * sigma, vwap - multiplier * sigma
        out["upper2"][a:b], out["lower2"][a:b] = vwap + 2 * multiplier * sigma, vwap - 2 * multiplier * sigma
    return out


@register("volume_profile", "Session Volume Profile (developing)", "volume", {"rows": L(24, 4, 100), "value_area": P(70.0, 10, 99, "float")},
          ("poc", "vah", "val"),
          "Volume by price over the session so far (typical price bins between the session low and high): POC = busiest price; "
          "value area = contiguous bins around the POC holding `value_area`% of volume. Recomputed from bars <= t, so it develops and never looks ahead.",
          lambda p: 2)
def _volume_profile(df, ctx, rows, value_area):
    h, l, c = _hlc(df)
    tp, v = (h + l + c) / 3, df["volume"].to_numpy(float)
    n = len(tp)
    poc, vah, val = (np.full(n, NAN) for _ in range(3))
    for a, b in _session_slices(df, ctx):
        lo, hi = l[a], h[a]
        for i in range(a, b):
            lo, hi = min(lo, l[i]), max(hi, h[i])
            if hi <= lo:
                continue
            hist = np.bincount(np.minimum(((tp[a:i + 1] - lo) / (hi - lo) * rows).astype(int), rows - 1), weights=v[a:i + 1], minlength=rows)
            total = hist.sum()
            if total <= 0:
                continue
            width = (hi - lo) / rows
            peak = int(hist.argmax())
            left = right = peak
            got = hist[peak]
            while got < total * value_area / 100 and (left > 0 or right < rows - 1):
                up = hist[right + 1] if right < rows - 1 else -1.0
                down = hist[left - 1] if left > 0 else -1.0
                if up >= down:
                    right += 1
                    got += hist[right]
                else:
                    left -= 1
                    got += hist[left]
            poc[i], val[i], vah[i] = lo + (peak + 0.5) * width, lo + left * width, lo + (right + 1) * width
    return {"poc": poc, "vah": vah, "val": val}


# ───────────────────────── structure and levels ─────────────────────────
_PIVOT_KINDS = ("classic", "fibonacci", "camarilla", "woodie")


@register("pivots", "Pivot Points (previous session)", "price_action", {"kind": P("classic", kind="choice", choices=_PIVOT_KINDS)},
          ("pp", "r1", "r2", "r3", "s1", "s2", "s3"),
          "Levels from the PREVIOUS completed session's high/low/close: classic, Fibonacci, Camarilla or Woodie. NaN on the first session.", lambda p: 1)
def _pivots(df, ctx, kind):
    keys = session_keys(df["time"], ctx.profile)
    g = df.assign(_k=keys.to_numpy()).groupby("_k", sort=True).agg(h=("high", "max"), l=("low", "min"), c=("close", "last"))
    prev = g.shift(1).reindex(keys.to_numpy())
    h, l, c = (prev[k].to_numpy(float) for k in ("h", "l", "c"))
    r = h - l
    if kind == "classic":
        pp = (h + l + c) / 3
        out = {"pp": pp, "r1": 2 * pp - l, "s1": 2 * pp - h, "r2": pp + r, "s2": pp - r, "r3": h + 2 * (pp - l), "s3": l - 2 * (h - pp)}
    elif kind == "fibonacci":
        pp = (h + l + c) / 3
        out = {"pp": pp, "r1": pp + 0.382 * r, "r2": pp + 0.618 * r, "r3": pp + r, "s1": pp - 0.382 * r, "s2": pp - 0.618 * r, "s3": pp - r}
    elif kind == "camarilla":
        out = {"pp": (h + l + c) / 3, "r1": c + r * 1.1 / 12, "r2": c + r * 1.1 / 6, "r3": c + r * 1.1 / 4,
               "s1": c - r * 1.1 / 12, "s2": c - r * 1.1 / 6, "s3": c - r * 1.1 / 4}
    else:  # woodie
        pp = (h + l + 2 * c) / 4
        out = {"pp": pp, "r1": 2 * pp - l, "s1": 2 * pp - h, "r2": pp + r, "s2": pp - r, "r3": h + 2 * (pp - l), "s3": l - 2 * (h - pp)}
    return out


@register("gap", "Opening Gap", "price_action", {}, ("gap_pct", "gap"), "session open vs previous session close: gap = open − prev close; gap_pct = 100·gap/prev close; constant within the session",
          lambda p: 1)
def _gap(df, ctx):
    keys = session_keys(df["time"], ctx.profile)
    g = df.assign(_k=keys.to_numpy()).groupby("_k", sort=True).agg(o=("open", "first"), c=("close", "last"))
    gap = (g["o"] - g["c"].shift(1)).reindex(keys.to_numpy()).to_numpy(float)
    prev_close = g["c"].shift(1).reindex(keys.to_numpy()).to_numpy(float)
    return {"gap": gap, "gap_pct": 100 * _div(gap, prev_close)}


def _confirmed_pivots(h: np.ndarray, l: np.ndarray, left: int, right: int):
    """Yield (confirm_row, pivot_row, 'high'|'low', price) in confirmation order; a pivot is known `right` bars after it formed."""
    w = left + right + 1
    if len(h) < w:
        return
    win_h, win_l = sliding_window_view(h, w), sliding_window_view(l, w)
    ph = (win_h[:, left] == win_h.max(axis=1)) & ((win_h[:, :left] < win_h[:, [left]]).all(axis=1) if left else True)
    pl = (win_l[:, left] == win_l.min(axis=1)) & ((win_l[:, :left] > win_l[:, [left]]).all(axis=1) if left else True)
    for k in range(len(win_h)):
        if ph[k]:
            yield k + w - 1, k + left, "high", win_h[k, left]
        if pl[k]:
            yield k + w - 1, k + left, "low", win_l[k, left]


@register("fib_retracement", "Fibonacci Retracement (confirmed swings)", "price_action", {"left": L(5, 1, 50), "right": L(5, 1, 50)},
          ("l236", "l382", "l500", "l618", "l786", "direction", "swing_high", "swing_low"),
          "Levels between the two most recent CONFIRMED swing points. After a swing low→high leg (direction +1) levels retrace down from the high; "
          "after a high→low leg (direction −1) they retrace up from the low.", lambda p: p["left"] + p["right"] + 2)
def _fib(df, ctx, left, right):
    h, l, _ = _hlc(df)
    n = len(h)
    keys = ("l236", "l382", "l500", "l618", "l786", "direction", "swing_high", "swing_low")
    out = {k: np.full(n, NAN) for k in keys}
    events: dict[int, list[tuple[int, str, float]]] = {}
    for confirm, pivot, side, price in _confirmed_pivots(h, l, left, right):
        events.setdefault(confirm, []).append((pivot, side, price))
    high = low = None   # (row, price)
    for i in range(n):
        for pivot, side, price in events.get(i, ()):
            if side == "high":
                high = (pivot, price)
            else:
                low = (pivot, price)
        if high is None or low is None or high[1] <= low[1]:
            continue
        up_leg = low[0] < high[0]
        span = high[1] - low[1]
        for name, ratio in zip(keys[:5], (0.236, 0.382, 0.5, 0.618, 0.786)):
            out[name][i] = high[1] - ratio * span if up_leg else low[1] + ratio * span
        out["direction"][i], out["swing_high"][i], out["swing_low"][i] = (1.0 if up_leg else -1.0), high[1], low[1]
    return out


@register("zigzag", "ZigZag (confirmed, non-repainting)", "price_action", {"percent": P(1.0, 0.05, 50, "float")}, ("pivot", "trend", "extreme"),
          "A pivot is confirmed when price reverses `percent`% from the running extreme. `pivot` is the confirmed pivot price, reported ON THE CONFIRMATION BAR only "
          "(never back-dated); `trend` +1/−1 is the current leg; `extreme` is the running high/low of the leg.", lambda p: 2)
def _zigzag(df, ctx, percent):
    h, l, _ = _hlc(df)
    n = len(h)
    pivot, trend, extreme = (np.full(n, NAN) for _ in range(3))
    if n < 2:
        return {"pivot": pivot, "trend": trend, "extreme": extreme}
    pct = percent / 100
    direction, top, bottom = 0, h[0], l[0]
    for i in range(1, n):
        if direction == 0:
            top, bottom = max(top, h[i]), min(bottom, l[i])
            if h[i] >= bottom * (1 + pct) and h[i] > l[0]:
                direction, top = 1, h[i]
            elif l[i] <= top * (1 - pct):
                direction, bottom = -1, l[i]
        elif direction == 1:
            if h[i] > top:
                top = h[i]
            elif l[i] <= top * (1 - pct):
                pivot[i], direction, bottom = top, -1, l[i]
        else:
            if l[i] < bottom:
                bottom = l[i]
            elif h[i] >= bottom * (1 + pct):
                pivot[i], direction, top = bottom, 1, h[i]
        if direction:
            trend[i] = float(direction)
            extreme[i] = top if direction == 1 else bottom
    return {"pivot": pivot, "trend": trend, "extreme": extreme}


# ───────────────────────── candlestick patterns ─────────────────────────
PATTERNS = (
    "doji", "dragonfly_doji", "gravestone_doji", "long_legged_doji", "spinning_top", "marubozu_bull", "marubozu_bear", "belt_hold_bull", "belt_hold_bear",
    "hammer", "inverted_hammer", "hanging_man", "shooting_star",
    "harami_bull", "harami_bear", "harami_cross_bull", "harami_cross_bear", "piercing_line", "dark_cloud_cover", "tweezer_bottom", "tweezer_top",
    "kicker_bull", "kicker_bear", "engulfing_bull", "engulfing_bear",
    "morning_star", "evening_star", "morning_doji_star", "evening_doji_star", "three_white_soldiers", "three_black_crows",
    "three_inside_up", "three_inside_down", "three_outside_up", "three_outside_down", "abandoned_baby_bull", "abandoned_baby_bear",
)


@register("cdl_patterns", "Candlestick Patterns", "price_action",
          {"doji_body": P(0.1, 0.01, 0.5, "float"), "long_body": P(0.6, 0.3, 0.95, "float"), "shadow_ratio": P(2.0, 1.0, 6.0, "float"), "trend_length": L(5, 1, 50)},
          PATTERNS + ("bullish_count", "bearish_count"),
          f"{len(PATTERNS)} patterns, each 1 on the bar that COMPLETES it and 0 otherwise. Body/shadow sizes are fractions of the bar's range; reversal patterns need the "
          "prior `trend_length`-bar move (close vs close n bars earlier). Uses only completed candles.", lambda p: p["trend_length"] + 4)
def _patterns(df, ctx, doji_body, long_body, shadow_ratio, trend_length):
    o, h, l, c = (df[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    n = len(c)
    rng = h - l
    body = np.abs(c - o)
    upper, lower = h - np.maximum(o, c), np.minimum(o, c) - l
    bull, bear = c > o, c < o
    mid = (o + c) / 2
    small = body <= doji_body * np.where(rng > 0, rng, np.nan)
    long_ = body >= long_body * np.where(rng > 0, rng, np.nan)
    down = np.r_[np.full(trend_length, False), c[trend_length:] < c[:-trend_length]] if n > trend_length else np.full(n, False)
    up = np.r_[np.full(trend_length, False), c[trend_length:] > c[:-trend_length]] if n > trend_length else np.full(n, False)

    def sh(a: np.ndarray, k: int) -> np.ndarray:
        out = np.full(n, False) if a.dtype == bool else np.full(n, np.nan)
        if k < n:
            out[k:] = a[:n - k]
        return out

    tiny = body <= 0.3 * np.where(rng > 0, rng, np.nan)
    safe_rng = np.where(rng > 0, rng, np.nan)
    small_body = body <= 0.35 * safe_rng
    hammer_shape = small_body & (lower >= np.maximum(shadow_ratio * body, 0.6 * safe_rng)) & (upper <= 0.1 * safe_rng)
    star_shape = small_body & (upper >= np.maximum(shadow_ratio * body, 0.6 * safe_rng)) & (lower <= 0.1 * safe_rng)
    p1 = {k: sh(v, 1) for k, v in dict(o=o, h=h, l=l, c=c, body=body, bull=bull, bear=bear, small=small, long_=long_, mid=mid, tiny=tiny, rng=rng).items()}
    p2 = {k: sh(v, 2) for k, v in dict(o=o, h=h, l=l, c=c, body=body, bull=bull, bear=bear, small=small, long_=long_, mid=mid, tiny=tiny).items()}
    inside_prev = (np.maximum(o, c) < np.maximum(p1["o"], p1["c"])) & (np.minimum(o, c) > np.minimum(p1["o"], p1["c"]))
    out: dict[str, np.ndarray] = {}
    out["doji"] = small
    out["dragonfly_doji"] = small & (lower >= 0.6 * rng) & (upper <= 0.1 * rng)
    out["gravestone_doji"] = small & (upper >= 0.6 * rng) & (lower <= 0.1 * rng)
    out["long_legged_doji"] = small & (upper >= 0.3 * rng) & (lower >= 0.3 * rng)
    out["spinning_top"] = (body > doji_body * rng) & (body <= 0.3 * rng) & (upper >= body) & (lower >= body)
    out["marubozu_bull"] = bull & (body >= 0.95 * rng)
    out["marubozu_bear"] = bear & (body >= 0.95 * rng)
    out["belt_hold_bull"] = bull & long_ & (lower <= 0.05 * rng) & down
    out["belt_hold_bear"] = bear & long_ & (upper <= 0.05 * rng) & up
    out["hammer"] = hammer_shape & down
    out["inverted_hammer"] = star_shape & down
    out["hanging_man"] = hammer_shape & up
    out["shooting_star"] = star_shape & up
    out["harami_bull"] = p1["bear"] & p1["long_"] & bull & inside_prev & down
    out["harami_bear"] = p1["bull"] & p1["long_"] & bear & inside_prev & up
    out["harami_cross_bull"] = p1["bear"] & p1["long_"] & small & inside_prev & down
    out["harami_cross_bear"] = p1["bull"] & p1["long_"] & small & inside_prev & up
    out["piercing_line"] = p1["bear"] & p1["long_"] & bull & (o < p1["l"]) & (c > p1["mid"]) & (c < p1["o"]) & down
    out["dark_cloud_cover"] = p1["bull"] & p1["long_"] & bear & (o > p1["h"]) & (c < p1["mid"]) & (c > p1["o"]) & up
    tol = 0.05 * np.where(rng > 0, rng, np.nan)
    out["tweezer_bottom"] = p1["bear"] & bull & (np.abs(l - p1["l"]) <= tol) & down
    out["tweezer_top"] = p1["bull"] & bear & (np.abs(h - p1["h"]) <= tol) & up
    out["kicker_bull"] = p1["bear"] & bull & (o >= p1["o"]) & long_ & p1["long_"]
    out["kicker_bear"] = p1["bull"] & bear & (o <= p1["o"]) & long_ & p1["long_"]
    out["engulfing_bull"] = p1["bear"] & bull & (o <= p1["c"]) & (c >= p1["o"]) & (body > p1["body"]) & down
    out["engulfing_bear"] = p1["bull"] & bear & (o >= p1["c"]) & (c <= p1["o"]) & (body > p1["body"]) & up
    out["morning_star"] = p2["bear"] & p2["long_"] & p1["tiny"] & (np.maximum(p1["o"], p1["c"]) < p2["c"]) & bull & (c > p2["mid"])
    out["evening_star"] = p2["bull"] & p2["long_"] & p1["tiny"] & (np.minimum(p1["o"], p1["c"]) > p2["c"]) & bear & (c < p2["mid"])
    out["morning_doji_star"] = out["morning_star"] & p1["small"]
    out["evening_doji_star"] = out["evening_star"] & p1["small"]
    out["three_white_soldiers"] = p2["bull"] & p1["bull"] & bull & (p1["c"] > p2["c"]) & (c > p1["c"]) & (p1["o"] > p2["o"]) & (o > p1["o"]) & (p1["o"] < p2["c"]) & (o < p1["c"]) & ~p2["small"] & ~p1["small"] & ~small
    out["three_black_crows"] = p2["bear"] & p1["bear"] & bear & (p1["c"] < p2["c"]) & (c < p1["c"]) & (p1["o"] < p2["o"]) & (o < p1["o"]) & (p1["o"] > p2["c"]) & (o > p1["c"]) & ~p2["small"] & ~p1["small"] & ~small
    harami_up = p2["bear"] & p2["long_"] & p1["bull"] & (p1["o"] > p2["c"]) & (p1["c"] < p2["o"])
    harami_dn = p2["bull"] & p2["long_"] & p1["bear"] & (p1["o"] < p2["c"]) & (p1["c"] > p2["o"])
    out["three_inside_up"] = harami_up & bull & (c > p2["o"])
    out["three_inside_down"] = harami_dn & bear & (c < p2["o"])
    eng_up = p2["bear"] & p1["bull"] & (p1["o"] <= p2["c"]) & (p1["c"] >= p2["o"])
    eng_dn = p2["bull"] & p1["bear"] & (p1["o"] >= p2["c"]) & (p1["c"] <= p2["o"])
    out["three_outside_up"] = eng_up & bull & (c > p1["c"])
    out["three_outside_down"] = eng_dn & bear & (c < p1["c"])
    out["abandoned_baby_bull"] = p2["bear"] & p2["long_"] & p1["small"] & (p1["h"] < p2["l"]) & bull & (l > p1["h"])
    out["abandoned_baby_bear"] = p2["bull"] & p2["long_"] & p1["small"] & (p1["l"] > p2["h"]) & bear & (h < p1["l"])
    first = trend_length + 3     # earlier rows lack the history the patterns need
    result = {}
    for name in PATTERNS:
        flag = np.where(np.isnan(np.asarray(out[name], dtype=float)), 0.0, np.asarray(out[name], dtype=float))
        flag = np.where(rng > 0, flag, 0.0)
        flag[:first] = NAN
        result[name] = flag
    bullish = [k for k in PATTERNS if k.endswith("_bull") or k in ("hammer", "inverted_hammer", "piercing_line", "tweezer_bottom", "morning_star", "morning_doji_star",
                                                                    "three_white_soldiers", "three_inside_up", "three_outside_up")]
    bearish = [k for k in PATTERNS if k.endswith("_bear") or k in ("hanging_man", "shooting_star", "dark_cloud_cover", "tweezer_top", "evening_star", "evening_doji_star",
                                                                    "three_black_crows", "three_inside_down", "three_outside_down")]
    result["bullish_count"] = np.sum([result[k] for k in bullish], axis=0)
    result["bearish_count"] = np.sum([result[k] for k in bearish], axis=0)
    return result


# ───────────────────────── statistics ─────────────────────────
@register("zscore", "Z-Score", "statistics", LEN, ("value",), "(x − SMA(x, n)) / σ(x, n)", lambda p: p["length"], True)
def _zscore(df, ctx, length, source="close"):
    x = src(df, source)
    return {"value": _div(x - sma(x, length), stdev(x, length))}


@register("percent_rank", "Percent Rank", "statistics", {"length": L(100, 2, 1000)}, ("value",),
          "share (0–100) of the previous n values that are ≤ the current value; the current bar is excluded", lambda p: p["length"] + 1, True)
def _prank(df, ctx, length, source="close"):
    return {"value": _percent_rank(src(df, source), length)}


@register("linreg_channel", "Linear Regression Channel", "statistics", {"length": L(100, 3, 1000), "multiplier": P(2.0, 0.1, 10, "float")},
          ("middle", "upper", "lower", "slope", "r2"), "end point of the least-squares line over n bars ± multiplier·(std dev of residuals); slope per bar; R² of the fit",
          lambda p: p["length"], True)
def _linreg_channel(df, ctx, length, multiplier, source="close"):
    y = src(df, source)
    n = len(y)
    names = ("middle", "upper", "lower", "slope", "r2")
    out = {k: np.full(n, NAN) for k in names}
    if n < length:
        return out
    xs = np.arange(length, dtype=float)
    xm = (length - 1) / 2
    cx = xs - xm
    denom = (cx ** 2).sum()
    win = sliding_window_view(y, length)
    mean = win.mean(axis=1)
    slope = (win * cx).sum(axis=1) / denom
    fitted = mean[:, None] + slope[:, None] * cx
    resid = win - fitted
    sd = resid.std(axis=1)
    ss_tot = ((win - mean[:, None]) ** 2).sum(axis=1)
    ss_res = (resid ** 2).sum(axis=1)
    end = mean + slope * (length - 1 - xm)
    out["middle"][length - 1:], out["slope"][length - 1:] = end, slope
    out["upper"][length - 1:], out["lower"][length - 1:] = end + multiplier * sd, end - multiplier * sd
    out["r2"][length - 1:] = np.where(ss_tot > 0, 1 - ss_res / np.where(ss_tot > 0, ss_tot, 1), NAN)
    return out


def _log_returns(x: np.ndarray) -> np.ndarray:
    out = np.full(len(x), NAN)
    with np.errstate(divide="ignore", invalid="ignore"):
        out[1:] = np.log(x[1:] / x[:-1])
    return out


@register("skewness", "Rolling Skewness (log returns)", "statistics", {"length": L(50, 4, 1000)}, ("value",), "third standardised moment of log returns over n", lambda p: p["length"] + 1, True)
def _skew(df, ctx, length, source="close"):
    def moment(w: np.ndarray) -> np.ndarray:
        d = w - w.mean(axis=1, keepdims=True)
        sd = np.sqrt((d ** 2).mean(axis=1))
        return np.where(sd > 0, (d ** 3).mean(axis=1) / np.where(sd > 0, sd, 1) ** 3, NAN)
    return {"value": _roll_apply(_log_returns(src(df, source)), length, moment)}


@register("kurtosis", "Rolling Excess Kurtosis (log returns)", "statistics", {"length": L(50, 4, 1000)}, ("value",), "fourth standardised moment − 3 of log returns over n", lambda p: p["length"] + 1, True)
def _kurt(df, ctx, length, source="close"):
    def moment(w: np.ndarray) -> np.ndarray:
        d = w - w.mean(axis=1, keepdims=True)
        var = (d ** 2).mean(axis=1)
        return np.where(var > 0, (d ** 4).mean(axis=1) / np.where(var > 0, var, 1) ** 2 - 3, NAN)
    return {"value": _roll_apply(_log_returns(src(df, source)), length, moment)}


@register("autocorr", "Return Autocorrelation (lag 1)", "statistics", {"length": L(50, 5, 1000)}, ("value",), "Pearson correlation of log return with the previous log return over n pairs",
          lambda p: p["length"] + 2, True)
def _autocorr(df, ctx, length, source="close"):
    r = _log_returns(src(df, source))
    out = np.full(len(r), NAN)
    if len(r) >= length + 1:
        a, b = sliding_window_view(r[1:], length), sliding_window_view(r[:-1], length)    # pairs (r_t, r_{t-1}); row 0 of r is NaN so the first usable pair is t=2
        da, db = a - a.mean(axis=1, keepdims=True), b - b.mean(axis=1, keepdims=True)
        denom = np.sqrt((da ** 2).sum(axis=1) * (db ** 2).sum(axis=1))
        out[length:] = np.where(denom > 0, (da * db).sum(axis=1) / np.where(denom > 0, denom, 1), NAN)
    out[: length + 1] = NAN
    return {"value": out}


@register("rsquared", "R² of Linear Trend", "statistics", {"length": L(30, 3, 1000)}, ("value",), "R² of a least-squares line fitted to source over n bars (trend straightness, 0–1)",
          lambda p: p["length"], True)
def _rsq(df, ctx, length, source="close"):
    return {"value": _linreg_channel(df, ctx, length, 1.0, source)["r2"]}


@register("rolling_sharpe", "Rolling Sharpe (per-bar returns)", "statistics", {"length": L(60, 5, 1000), "annualization": P(1, 1, 1_000_000, "float")}, ("value",),
          "mean(simple return)/stdev(simple return) over n × √annualization; risk-free rate 0", lambda p: p["length"] + 1, True)
def _rsharpe(df, ctx, length, annualization, source="close"):
    x = src(df, source)
    ret = np.full(len(x), NAN)
    ret[1:] = x[1:] / np.where(x[:-1] != 0, x[:-1], NAN) - 1
    mean, sd = sma(ret, length), stdev(ret, length)
    return {"value": _div(mean, sd) * math.sqrt(annualization)}


@register("hurst", "Hurst Exponent (variance of lagged differences)", "statistics", {"length": L(100, 30, 1000), "max_lag": L(20, 3, 100)}, ("value",),
          "slope of log σ(x_t − x_{t−lag}) vs log lag over lags 2..max_lag: ≈0.5 random walk, >0.5 trending, <0.5 mean-reverting", lambda p: p["length"], True)
def _hurst(df, ctx, length, max_lag, source="close"):
    x = src(df, source)
    n = len(x)
    out = np.full(n, NAN)
    max_lag = min(max_lag, length - 2)
    if n < length or max_lag < 3:
        return {"value": out}
    win = sliding_window_view(x, length)
    lags = np.arange(2, max_lag + 1)
    tau = np.column_stack([np.std(win[:, k:] - win[:, :-k], axis=1) for k in lags])
    with np.errstate(divide="ignore", invalid="ignore"):
        logs = np.log(tau)
    ll = np.log(lags)
    cl = ll - ll.mean()
    with np.errstate(invalid="ignore"):
        slope = (logs - logs.mean(axis=1, keepdims=True)) @ cl / (cl ** 2).sum()
    out[length - 1:] = np.where(np.isfinite(slope), slope, NAN)
    return {"value": out}


# ───────────────────────── alert-candle state machines ─────────────────────────
@register("ema_alert_candle", "EMA Alert Candle (5-EMA setup)", "price_action", {"length": L(5, 2, 200)},
          ("short_trigger", "long_trigger", "short_stop", "long_stop", "ema"),
          "Alert candle: a candle whose range does not touch the EMA (low > EMA = sell alert; high < EMA = buy alert). A later alert replaces an earlier one that has not triggered. "
          "Trigger: a candle whose low breaks below the sell-alert candle's low (high above the buy-alert's high) - 1 on that bar, and the alert is then used up. "
          "Stop levels: the sell alert's HIGH / the buy alert's LOW (valid on trigger bars). Uses completed candles only.", lambda p: p["length"] + 1)
def _ema_alert(df, ctx, length):
    h, l, c = _hlc(df)
    e = ema(c, length)
    n = len(c)
    short_trigger, long_trigger, short_stop, long_stop = (np.full(n, NAN) for _ in range(4))
    sell: tuple[float, float] | None = None      # (alert low, alert high)
    buy: tuple[float, float] | None = None       # (alert high, alert low)
    for i in range(n):
        if math.isnan(e[i]):
            continue
        short_trigger[i] = long_trigger[i] = 0.0
        if sell is not None and l[i] < sell[0]:
            short_trigger[i], short_stop[i], sell = 1.0, sell[1], None
        elif sell is not None:
            short_stop[i] = sell[1]
        if buy is not None and h[i] > buy[0]:
            long_trigger[i], long_stop[i], buy = 1.0, buy[1], None
        elif buy is not None:
            long_stop[i] = buy[1]
        if short_trigger[i] != 1.0 and l[i] > e[i]:
            sell = (l[i], h[i])                  # this candle does not touch the EMA from above: it is (or replaces) the sell alert
            short_stop[i] = h[i]
        if long_trigger[i] != 1.0 and h[i] < e[i]:
            buy = (h[i], l[i])
            long_stop[i] = l[i]
    return {"short_trigger": short_trigger, "long_trigger": long_trigger, "short_stop": short_stop, "long_stop": long_stop, "ema": e}
