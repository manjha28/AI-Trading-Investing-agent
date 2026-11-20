"""Intraday indicator library: one registry used by the visual builder, Pine import, Python sandbox, live signals and backtests.

Every indicator is a pure function of the candles up to and including the current row (no centred windows, no negative shifts), so a
value at row *t* never depends on rows after *t*. Warm-up rows are NaN, never estimated. Recursive smoothing (EMA, Wilder RMA) is
seeded with the SMA of the first ``length`` valid values (TradingView/Pine convention). Statistics use population standard deviation.
These are float statistical transforms (AGENTS.md): they never become money without a Decimal boundary.

The older ``indicator_library`` module serves the frozen daily strategies and keeps its original warm-up semantics; it is not used here.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view

from app.services.timeframes import MarketProfile, minutes_of, session_keys

INDICATOR_LIBRARY_VERSION = "1.2.0"
SOURCES = ("close", "open", "high", "low", "volume", "hl2", "hlc3", "ohlc4")
NAN = float("nan")


@dataclass(frozen=True)
class P:
    default: float | int | str
    low: float | None = None
    high: float | None = None
    kind: str = "int"  # int | float | choice | text
    choices: tuple[str, ...] = ()


@dataclass(frozen=True)
class Ctx:
    profile: MarketProfile
    timeframe: str = "1m"


@dataclass(frozen=True)
class Indicator:
    id: str
    name: str
    category: str
    params: dict[str, P]
    outputs: tuple[str, ...]
    formula: str
    warmup: Callable[[dict[str, Any]], int]
    fn: Callable[..., dict[str, np.ndarray]]
    uses_source: bool = False
    notes: str = ""


REGISTRY: dict[str, Indicator] = {}


def _source_param() -> P:
    return P("close", kind="choice", choices=SOURCES)


def register(id: str, name: str, category: str, params: dict[str, P], outputs: tuple[str, ...], formula: str,
             warmup: Callable[[dict[str, Any]], int], uses_source: bool = False, notes: str = "") -> Callable:
    def deco(fn: Callable) -> Callable:
        p = {**params, "source": _source_param()} if uses_source else params
        REGISTRY[id] = Indicator(id, name, category, p, outputs, formula, warmup, fn, uses_source, notes)
        return fn
    return deco


# ───────────────────────── primitives ─────────────────────────
def src(df: pd.DataFrame, name: str = "close") -> np.ndarray:
    o, h, l, c = (df[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    return {"close": c, "open": o, "high": h, "low": l, "volume": df["volume"].to_numpy(float), "hl2": (h + l) / 2,
            "hlc3": (h + l + c) / 3, "ohlc4": (o + h + l + c) / 4}[name]


def _roll_apply(x: np.ndarray, n: int, fn: Callable[[np.ndarray], np.ndarray]) -> np.ndarray:
    out = np.full(len(x), NAN)
    if len(x) >= n:
        out[n - 1:] = fn(sliding_window_view(x, n))
    return out


def sma(x: np.ndarray, n: int) -> np.ndarray:
    return _roll_apply(x, n, lambda w: w.mean(axis=1))


def _smooth(x: np.ndarray, n: int, alpha: float) -> np.ndarray:
    """Recursive smoothing seeded with the SMA of the first n consecutive valid values; NaN before the seed, held through later NaNs."""
    out = np.full(len(x), NAN)
    ok = ~np.isnan(x)
    if len(x) < n or ok.sum() < n:
        return out
    run = np.convolve(ok.astype(int), np.ones(n, dtype=int), mode="full")[n - 1:len(x)]
    starts = np.flatnonzero(run == n)
    if len(starts) == 0:
        return out
    seed = int(starts[0]) + n - 1
    out[seed] = x[seed - n + 1:seed + 1].mean()
    prev = out[seed]
    for i in range(seed + 1, len(x)):
        if not math.isnan(x[i]):
            prev = prev + alpha * (x[i] - prev)
        out[i] = prev
    return out


def ema(x: np.ndarray, n: int) -> np.ndarray:
    return _smooth(x, n, 2.0 / (n + 1))


def rma(x: np.ndarray, n: int) -> np.ndarray:
    return _smooth(x, n, 1.0 / n)


def wma(x: np.ndarray, n: int) -> np.ndarray:
    w = np.arange(1, n + 1, dtype=float)
    return _roll_apply(x, n, lambda win: win @ w / w.sum())


def rolling_max(x: np.ndarray, n: int) -> np.ndarray:
    return _roll_apply(x, n, lambda w: w.max(axis=1))


def rolling_min(x: np.ndarray, n: int) -> np.ndarray:
    return _roll_apply(x, n, lambda w: w.min(axis=1))


def rolling_sum(x: np.ndarray, n: int) -> np.ndarray:
    return _roll_apply(x, n, lambda w: w.sum(axis=1))


def stdev(x: np.ndarray, n: int) -> np.ndarray:
    return _roll_apply(x, n, lambda w: w.std(axis=1))  # population (ddof=0)


def shift(x: np.ndarray, k: int) -> np.ndarray:
    out = np.full(len(x), NAN)
    if k >= 0:
        out[k:] = x[:len(x) - k] if k else x
        return out
    raise ValueError("negative shifts would leak future data")


def true_range(h: np.ndarray, l: np.ndarray, c: np.ndarray) -> np.ndarray:
    pc = shift(c, 1)
    tr = np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))
    tr[0] = h[0] - l[0]
    return tr


def _hlc(df: pd.DataFrame) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    return df["high"].to_numpy(float), df["low"].to_numpy(float), df["close"].to_numpy(float)


def _div(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(b != 0, a / b, NAN)


L = lambda d, lo=1, hi=500: P(d, lo, hi)  # noqa: E731
LEN = {"length": L(20)}
LEN14 = {"length": L(14)}


# ───────────────────────── trend ─────────────────────────
@register("sma", "Simple Moving Average", "trend", LEN, ("value",), "mean(source, length)", lambda p: p["length"], True)
def _sma(df, ctx, length, source="close"): return {"value": sma(src(df, source), length)}


@register("ema", "Exponential Moving Average", "trend", LEN, ("value",), "EMA_t = EMA_{t-1} + 2/(n+1)·(x_t − EMA_{t-1}), seeded with SMA(n)", lambda p: p["length"], True)
def _ema(df, ctx, length, source="close"): return {"value": ema(src(df, source), length)}


@register("wma", "Weighted Moving Average", "trend", LEN, ("value",), "Σ i·x_{t-n+i} / Σ i", lambda p: p["length"], True)
def _wma(df, ctx, length, source="close"): return {"value": wma(src(df, source), length)}


@register("vwma", "Volume Weighted Moving Average", "trend", LEN, ("value",), "Σ(close·volume) / Σ volume over n", lambda p: p["length"])
def _vwma(df, ctx, length):
    c, v = df["close"].to_numpy(float), df["volume"].to_numpy(float)
    return {"value": _div(rolling_sum(c * v, length), rolling_sum(v, length))}


@register("hma", "Hull Moving Average", "trend", LEN, ("value",), "WMA(2·WMA(x, n/2) − WMA(x, n), √n)",
          lambda p: p["length"] + int(round(math.sqrt(p["length"]))) - 1, True)
def _hma(df, ctx, length, source="close"):
    x = src(df, source)
    raw = 2 * wma(x, max(1, length // 2)) - wma(x, length)
    return {"value": wma(raw, max(1, int(round(math.sqrt(length)))))}


@register("supertrend", "Supertrend", "trend", {"length": L(10), "multiplier": P(3.0, 0.1, 20, "float")}, ("line", "direction"),
          "band = hl2 ± m·ATR(n); line follows the band on the side of the trend; direction +1 up / −1 down", lambda p: p["length"])
def _supertrend(df, ctx, length, multiplier):
    h, l, c = _hlc(df)
    atr_ = rma(true_range(h, l, c), length)
    hl2 = (h + l) / 2
    ub, lb = hl2 + multiplier * atr_, hl2 - multiplier * atr_
    n = len(c)
    line, direction = np.full(n, NAN), np.full(n, NAN)
    fu, fl, d = NAN, NAN, 1
    for i in range(n):
        if math.isnan(atr_[i]):
            continue
        if math.isnan(fu):
            fu, fl, d = ub[i], lb[i], 1
        else:
            fu = ub[i] if (ub[i] < fu or c[i - 1] > fu) else fu
            fl = lb[i] if (lb[i] > fl or c[i - 1] < fl) else fl
            d = 1 if c[i] > fu else (-1 if c[i] < fl else d)
        line[i], direction[i] = (fl if d == 1 else fu), d
    return {"line": line, "direction": direction}


@register("adx", "Average Directional Index", "trend", LEN14, ("adx", "plus_di", "minus_di"),
          "+DI/−DI = 100·RMA(±DM)/RMA(TR); DX = 100·|+DI−−DI|/(+DI+−DI); ADX = RMA(DX, n)", lambda p: 2 * p["length"])
def _adx(df, ctx, length):
    h, l, c = _hlc(df)
    up, dn = h - shift(h, 1), shift(l, 1) - l
    plus = np.where((up > dn) & (up > 0), up, 0.0)
    minus = np.where((dn > up) & (dn > 0), dn, 0.0)
    plus[0] = minus[0] = NAN
    tr = true_range(h, l, c)
    tr_s = rma(np.where(np.isnan(plus), NAN, tr), length)
    pdi, mdi = 100 * _div(rma(plus, length), tr_s), 100 * _div(rma(minus, length), tr_s)
    dx = 100 * _div(np.abs(pdi - mdi), pdi + mdi)
    return {"adx": rma(dx, length), "plus_di": pdi, "minus_di": mdi}


@register("psar", "Parabolic SAR", "trend", {"step": P(0.02, 0.001, 1, "float"), "max_step": P(0.2, 0.01, 1, "float")}, ("sar", "direction"),
          "SAR_{t+1} = SAR_t + AF·(EP − SAR_t); AF steps on new extremes up to max_step; flips when price crosses SAR", lambda p: 2)
def _psar(df, ctx, step, max_step):
    h, l, _ = _hlc(df)
    n = len(h)
    sar, direction = np.full(n, NAN), np.full(n, NAN)
    if n < 2:
        return {"sar": sar, "direction": direction}
    up = h[1] >= h[0]
    s, ep, af = (l[0], h[0], step) if up else (h[0], l[0], step)
    sar[1], direction[1] = s, 1 if up else -1
    for i in range(2, n):
        s = s + af * (ep - s)
        if up:
            s = min(s, l[i - 1], l[i - 2])
            if l[i] < s:
                up, s, ep, af = False, ep, l[i], step
            elif h[i] > ep:
                ep, af = h[i], min(af + step, max_step)
        else:
            s = max(s, h[i - 1], h[i - 2])
            if h[i] > s:
                up, s, ep, af = True, ep, h[i], step
            elif l[i] < ep:
                ep, af = l[i], min(af + step, max_step)
        sar[i], direction[i] = s, 1 if up else -1
    return {"sar": sar, "direction": direction}


@register("ichimoku", "Ichimoku Cloud", "trend", {"conversion": L(9), "base": L(26), "span_b": L(52), "displacement": L(26, 1, 100)},
          ("tenkan", "kijun", "span_a", "span_b"),
          "tenkan=(HH+LL)/2(9); kijun=(HH+LL)/2(26); span_a=(tenkan+kijun)/2 and span_b=(HH+LL)/2(52), both plotted `displacement` bars later",
          lambda p: max(p["base"], p["span_b"]) + p["displacement"],
          notes="Spans use the value computed `displacement` bars ago, so no future data is used. The lagging span (chikou) is intentionally omitted: it is plotted into the past and would leak the future.")
def _ichimoku(df, ctx, conversion, base, span_b, displacement):
    h, l, _ = _hlc(df)
    mid = lambda n: (rolling_max(h, n) + rolling_min(l, n)) / 2  # noqa: E731
    t, k = mid(conversion), mid(base)
    return {"tenkan": t, "kijun": k, "span_a": shift((t + k) / 2, displacement), "span_b": shift(mid(span_b), displacement)}


@register("donchian", "Donchian Channels", "trend", LEN, ("upper", "lower", "middle"), "upper=max(high,n); lower=min(low,n); middle=(upper+lower)/2", lambda p: p["length"])
def _donchian(df, ctx, length):
    h, l, _ = _hlc(df)
    u, lo = rolling_max(h, length), rolling_min(l, length)
    return {"upper": u, "lower": lo, "middle": (u + lo) / 2}


@register("aroon", "Aroon", "trend", LEN14, ("up", "down", "oscillator"), "Up=100·(n − bars since highest high)/n over n+1 bars; Down likewise; Osc=Up−Down", lambda p: p["length"] + 1)
def _aroon(df, ctx, length):
    h, l, _ = _hlc(df)
    up = _roll_apply(h, length + 1, lambda w: 100.0 * (length - np.argmax(w[:, ::-1], axis=1)) / length)
    dn = _roll_apply(l, length + 1, lambda w: 100.0 * (length - np.argmin(w[:, ::-1], axis=1)) / length)
    return {"up": up, "down": dn, "oscillator": up - dn}


@register("keltner", "Keltner Channels", "trend", {"length": L(20), "multiplier": P(2.0, 0.1, 20, "float"), "atr_length": L(10)}, ("middle", "upper", "lower"),
          "middle=EMA(close,n); bands=middle ± m·ATR(atr_length)", lambda p: max(p["length"], p["atr_length"] + 1))
def _keltner(df, ctx, length, multiplier, atr_length):
    h, l, c = _hlc(df)
    mid, a = ema(c, length), rma(true_range(h, l, c), atr_length)
    return {"middle": mid, "upper": mid + multiplier * a, "lower": mid - multiplier * a}


# ───────────────────────── momentum ─────────────────────────
def _rsi(x: np.ndarray, n: int) -> np.ndarray:
    d = np.diff(x, prepend=NAN)
    up, dn = np.where(d > 0, d, 0.0), np.where(d < 0, -d, 0.0)
    up[0] = dn[0] = NAN
    ru, rd = rma(up, n), rma(dn, n)
    with np.errstate(divide="ignore", invalid="ignore"):
        out = np.where(rd == 0, np.where(ru == 0, 50.0, 100.0), 100 - 100 / (1 + ru / rd))
    return np.where(np.isnan(ru) | np.isnan(rd), NAN, out)


@register("rsi", "Relative Strength Index", "momentum", LEN14, ("value",), "RSI = 100 − 100/(1 + RMA(gain,n)/RMA(loss,n))", lambda p: p["length"] + 1, True)
def _rsi_i(df, ctx, length, source="close"): return {"value": _rsi(src(df, source), length)}


def _stoch_raw(h, l, c, n):
    hh, ll = rolling_max(h, n), rolling_min(l, n)
    return 100 * _div(c - ll, hh - ll)


@register("stoch", "Stochastic Oscillator", "momentum", {"k_length": L(14), "k_smooth": L(3), "d_length": L(3)}, ("k", "d"),
          "%K = SMA(100·(close−LL)/(HH−LL), k_smooth); %D = SMA(%K, d_length)", lambda p: p["k_length"] + p["k_smooth"] + p["d_length"] - 2)
def _stoch(df, ctx, k_length, k_smooth, d_length):
    h, l, c = _hlc(df)
    k = sma(_stoch_raw(h, l, c, k_length), k_smooth)
    return {"k": k, "d": sma(k, d_length)}


@register("stochrsi", "Stochastic RSI", "momentum", {"rsi_length": L(14), "stoch_length": L(14), "k_smooth": L(3), "d_length": L(3)}, ("k", "d"),
          "Stochastic of RSI over stoch_length, %K=SMA(k_smooth), %D=SMA(d_length)", lambda p: p["rsi_length"] + p["stoch_length"] + p["k_smooth"] + p["d_length"] - 2, True)
def _stochrsi(df, ctx, rsi_length, stoch_length, k_smooth, d_length, source="close"):
    r = _rsi(src(df, source), rsi_length)
    k = sma(100 * _div(r - rolling_min(r, stoch_length), rolling_max(r, stoch_length) - rolling_min(r, stoch_length)), k_smooth)
    return {"k": k, "d": sma(k, d_length)}


@register("macd", "MACD", "momentum", {"fast": L(12), "slow": L(26), "signal": L(9)}, ("macd", "signal", "hist"), "MACD=EMA(fast)−EMA(slow); signal=EMA(MACD,signal); hist=MACD−signal",
          lambda p: p["slow"] + p["signal"] - 1, True)
def _macd(df, ctx, fast, slow, signal, source="close"):
    x = src(df, source)
    m = ema(x, fast) - ema(x, slow)
    s = ema(m, signal)
    return {"macd": m, "signal": s, "hist": m - s}


@register("cci", "Commodity Channel Index", "momentum", LEN, ("value",), "(TP − SMA(TP,n)) / (0.015·meanAbsDev(TP,n)), TP=hlc3", lambda p: p["length"])
def _cci(df, ctx, length):
    tp = src(df, "hlc3")
    ma = sma(tp, length)
    md = _roll_apply(tp, length, lambda w: np.abs(w - w.mean(axis=1, keepdims=True)).mean(axis=1))
    return {"value": _div(tp - ma, 0.015 * md)}


@register("roc", "Rate of Change", "momentum", LEN, ("value",), "100·(x_t / x_{t−n} − 1)", lambda p: p["length"] + 1, True)
def _roc(df, ctx, length, source="close"):
    x = src(df, source)
    return {"value": 100 * (_div(x, shift(x, length)) - 1)}


@register("momentum", "Momentum", "momentum", {"length": L(10)}, ("value",), "x_t − x_{t−n}", lambda p: p["length"] + 1, True)
def _mom(df, ctx, length, source="close"):
    x = src(df, source)
    return {"value": x - shift(x, length)}


@register("willr", "Williams %R", "momentum", LEN14, ("value",), "−100·(HH − close)/(HH − LL)", lambda p: p["length"])
def _willr(df, ctx, length):
    h, l, c = _hlc(df)
    hh, ll = rolling_max(h, length), rolling_min(l, length)
    return {"value": -100 * _div(hh - c, hh - ll)}


@register("mfi", "Money Flow Index", "momentum", LEN14, ("value",), "100 − 100/(1 + Σ positive flow / Σ negative flow), flow = hlc3·volume", lambda p: p["length"] + 1)
def _mfi(df, ctx, length):
    tp, v = src(df, "hlc3"), df["volume"].to_numpy(float)
    d = np.diff(tp, prepend=NAN)
    flow = tp * v
    pos, neg = np.where(d > 0, flow, 0.0), np.where(d < 0, flow, 0.0)
    pos[0] = neg[0] = NAN
    ps, ns = rolling_sum(pos, length), rolling_sum(neg, length)
    with np.errstate(divide="ignore", invalid="ignore"):
        out = np.where(ns == 0, np.where(ps == 0, 50.0, 100.0), 100 - 100 / (1 + ps / ns))
    return {"value": np.where(np.isnan(ps) | np.isnan(ns), NAN, out)}


@register("trix", "TRIX", "momentum", {"length": L(15)}, ("value",), "100·(EMA3_t / EMA3_{t−1} − 1), EMA3 = triple-smoothed EMA", lambda p: 3 * p["length"] - 1, True)
def _trix(df, ctx, length, source="close"):
    e = ema(ema(ema(src(df, source), length), length), length)
    return {"value": 100 * (_div(e, shift(e, 1)) - 1)}


# ───────────────────────── volatility ─────────────────────────
@register("atr", "Average True Range", "volatility", LEN14, ("value",), "RMA(max(H−L, |H−C₋₁|, |L−C₋₁|), n)", lambda p: p["length"])
def _atr(df, ctx, length):
    h, l, c = _hlc(df)
    return {"value": rma(true_range(h, l, c), length)}


@register("bbands", "Bollinger Bands", "volatility", {"length": L(20), "multiplier": P(2.0, 0.1, 10, "float")}, ("middle", "upper", "lower", "width", "percent_b"),
          "middle=SMA; bands=middle ± m·σ (population); width=(upper−lower)/middle; %B=(x−lower)/(upper−lower)", lambda p: p["length"], True)
def _bb(df, ctx, length, multiplier, source="close"):
    x = src(df, source)
    mid, sd = sma(x, length), stdev(x, length)
    up, lo = mid + multiplier * sd, mid - multiplier * sd
    return {"middle": mid, "upper": up, "lower": lo, "width": _div(up - lo, mid), "percent_b": _div(x - lo, up - lo)}


@register("stdev", "Standard Deviation", "volatility", LEN, ("value",), "population standard deviation of source over n", lambda p: p["length"], True)
def _sd(df, ctx, length, source="close"): return {"value": stdev(src(df, source), length)}


@register("hist_vol", "Historical Volatility", "volatility", {"length": L(20, 2, 500), "annualization": P(1, 1, 1_000_000, "float")}, ("value",),
          "100·σ(ln(close_t/close_{t−1}), n)·√annualization (annualization=1 → per-bar volatility, %)", lambda p: p["length"] + 1)
def _hv(df, ctx, length, annualization):
    c = df["close"].to_numpy(float)
    r = np.log(_div(c, shift(c, 1)))
    return {"value": 100 * stdev(r, length) * math.sqrt(annualization)}


@register("choppiness", "Choppiness Index", "volatility", LEN14, ("value",), "100·log10(Σ TR(n) / (HH−LL)) / log10(n)", lambda p: p["length"])
def _chop(df, ctx, length):
    h, l, c = _hlc(df)
    rng = rolling_max(h, length) - rolling_min(l, length)
    with np.errstate(divide="ignore", invalid="ignore"):
        v = 100 * np.log10(rolling_sum(true_range(h, l, c), length) / rng) / math.log10(length)
    return {"value": np.where(rng > 0, v, NAN)}


# ───────────────────────── volume ─────────────────────────
@register("vwap", "VWAP (session)", "volume", {}, ("value",), "cumΣ(hlc3·volume)/cumΣ volume, reset each session (IST date for NSE, UTC date for crypto)", lambda p: 1)
def _vwap(df, ctx):
    tp, v = src(df, "hlc3"), df["volume"].to_numpy(float)
    keys = session_keys(df["time"], ctx.profile).to_numpy()
    out = np.full(len(df), NAN)
    start = 0
    for i in range(1, len(df) + 1):
        if i == len(df) or keys[i] != keys[start]:
            pv, vv = np.cumsum(tp[start:i] * v[start:i]), np.cumsum(v[start:i])
            out[start:i] = _div(pv, vv)
            start = i
    return {"value": out}


@register("anchored_vwap", "Anchored VWAP", "volume", {"anchor": P("", kind="text")}, ("value",),
          "cumΣ(hlc3·volume)/cumΣ volume from the anchor timestamp (ISO-8601, UTC) onward; NaN before it", lambda p: 1,
          notes="Requires an explicit anchor timestamp; the anchor must be earlier than the evaluated bar.")
def _avwap(df, ctx, anchor):
    out = np.full(len(df), NAN)
    if not anchor:
        return {"value": out}
    ts = pd.Timestamp(anchor)
    ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
    mask = (df["time"] >= ts).to_numpy()
    tp, v = src(df, "hlc3"), df["volume"].to_numpy(float)
    out[mask] = _div(np.cumsum((tp * v)[mask]), np.cumsum(v[mask]))
    return {"value": out}


@register("obv", "On-Balance Volume", "volume", {}, ("value",), "OBV_t = OBV_{t−1} + sign(close_t − close_{t−1})·volume_t", lambda p: 2)
def _obv(df, ctx):
    c, v = df["close"].to_numpy(float), df["volume"].to_numpy(float)
    s = np.sign(np.diff(c, prepend=c[0] if len(c) else 0.0))
    return {"value": np.cumsum(s * v)}


@register("vol_sma", "Volume SMA", "volume", LEN, ("value",), "SMA(volume, n)", lambda p: p["length"])
def _vsma(df, ctx, length): return {"value": sma(df["volume"].to_numpy(float), length)}


@register("vol_ema", "Volume EMA", "volume", LEN, ("value",), "EMA(volume, n)", lambda p: p["length"])
def _vema(df, ctx, length): return {"value": ema(df["volume"].to_numpy(float), length)}


@register("vol_roc", "Volume Rate of Change", "volume", {"length": L(14)}, ("value",), "100·(volume_t / volume_{t−n} − 1)", lambda p: p["length"] + 1)
def _vroc(df, ctx, length):
    v = df["volume"].to_numpy(float)
    return {"value": 100 * (_div(v, shift(v, length)) - 1)}


@register("ad", "Accumulation/Distribution", "volume", {}, ("value",), "cumΣ ((C−L)−(H−C))/(H−L)·volume", lambda p: 1)
def _ad(df, ctx):
    h, l, c = _hlc(df)
    mfm = _div((c - l) - (h - c), h - l)
    return {"value": np.cumsum(np.nan_to_num(mfm) * df["volume"].to_numpy(float))}


@register("cmf", "Chaikin Money Flow", "volume", {"length": L(20)}, ("value",), "Σ(MFM·volume, n) / Σ(volume, n)", lambda p: p["length"])
def _cmf(df, ctx, length):
    h, l, c = _hlc(df)
    v = df["volume"].to_numpy(float)
    return {"value": _div(rolling_sum(np.nan_to_num(_div((c - l) - (h - c), h - l)) * v, length), rolling_sum(v, length))}


@register("rvol", "Relative Volume", "volume", {"length": L(20)}, ("value",), "volume_t / SMA(volume over the previous n bars, excluding the current bar)", lambda p: p["length"] + 1)
def _rvol(df, ctx, length):
    v = df["volume"].to_numpy(float)
    return {"value": _div(v, shift(sma(v, length), 1))}


# ───────────────────────── price action / structure ─────────────────────────
@register("prev_candle", "Previous candle high/low/close", "price_action", {}, ("high", "low", "close"), "OHLC of the immediately preceding completed candle of this timeframe", lambda p: 2)
def _prev(df, ctx):
    return {k: shift(df[k].to_numpy(float), 1) for k in ("high", "low", "close")}


@register("prev_day", "Previous day high/low/close", "price_action", {}, ("high", "low", "close"), "high/low/close of the previous session (IST date for NSE, UTC date for crypto)", lambda p: 2)
def _prev_day(df, ctx):
    keys = session_keys(df["time"], ctx.profile)
    g = df.assign(_k=keys.to_numpy()).groupby("_k", sort=True).agg(high=("high", "max"), low=("low", "min"), close=("close", "last"))
    prev = g.shift(1)
    mapped = prev.reindex(keys.to_numpy())
    return {k: mapped[k].to_numpy(float) for k in ("high", "low", "close")}


@register("opening_range", "Opening range high/low", "price_action", {"minutes": P(15, 1, 240), "session_start": P("", kind="text")}, ("high", "low"),
          "high/low of candles completed within the first `minutes` of the session; NaN until the range is complete",
          lambda p: 1, notes="Equity: measured from the 09:15 IST open. Crypto has no exchange open: set session_start (HH:MM, UTC) to define the range; "
                             "blank means 00:00 UTC. A non-blank session_start also overrides the equity open.")
def _orange(df, ctx, minutes, session_start=""):
    keys = session_keys(df["time"], ctx.profile).to_numpy()
    local = df["time"].dt.tz_convert(ctx.profile.tz)
    start_min = ctx.profile.open_minute
    if session_start:
        try:
            start_min = int(session_start[:2]) * 60 + int(session_start[3:5])
        except ValueError:
            raise IndicatorError("opening_range.session_start must be HH:MM") from None
    rel = (local.dt.hour * 60 + local.dt.minute - start_min).to_numpy()
    width = minutes_of(ctx.timeframe)
    hi, lo = np.full(len(df), NAN), np.full(len(df), NAN)
    h, l = df["high"].to_numpy(float), df["low"].to_numpy(float)
    i = 0
    n = len(df)
    while i < n:
        j = i
        while j < n and keys[j] == keys[i]:
            j += 1
        in_range = (rel[i:j] >= 0) & (rel[i:j] + width <= minutes)
        after = rel[i:j] >= minutes
        if in_range.any():
            hi[i:j][after] = h[i:j][in_range].max()
            lo[i:j][after] = l[i:j][in_range].min()
        i = j
    return {"high": hi, "low": lo}


@register("candle_shape", "Candle body/wick ratios", "price_action", {}, ("body_ratio", "upper_wick_ratio", "lower_wick_ratio", "direction"),
          "body=|C−O|/(H−L); upper wick=(H−max(O,C))/(H−L); lower wick=(min(O,C)−L)/(H−L); direction=+1 bull/−1 bear/0 doji", lambda p: 1)
def _shape(df, ctx):
    o, h, l, c = (df[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    r = h - l
    return {"body_ratio": _div(np.abs(c - o), r), "upper_wick_ratio": _div(h - np.maximum(o, c), r),
            "lower_wick_ratio": _div(np.minimum(o, c) - l, r), "direction": np.sign(c - o)}


@register("inside_bar", "Inside bar", "price_action", {}, ("value",), "1 when high ≤ previous high and low ≥ previous low", lambda p: 2)
def _inside(df, ctx):
    h, l, _ = _hlc(df)
    ph, pl = shift(h, 1), shift(l, 1)
    return {"value": np.where(np.isnan(ph), NAN, ((h <= ph) & (l >= pl)).astype(float))}


@register("outside_bar", "Outside bar", "price_action", {}, ("value",), "1 when high > previous high and low < previous low", lambda p: 2)
def _outside(df, ctx):
    h, l, _ = _hlc(df)
    ph, pl = shift(h, 1), shift(l, 1)
    return {"value": np.where(np.isnan(ph), NAN, ((h > ph) & (l < pl)).astype(float))}


@register("engulfing", "Engulfing pattern", "price_action", {}, ("bullish", "bearish"),
          "bullish: bearish prior body fully engulfed by this bullish body; bearish is the mirror", lambda p: 2)
def _engulf(df, ctx):
    o, c = df["open"].to_numpy(float), df["close"].to_numpy(float)
    po, pc = shift(o, 1), shift(c, 1)
    bull = (pc < po) & (c > o) & (c >= po) & (o <= pc)
    bear = (pc > po) & (c < o) & (c <= po) & (o >= pc)
    nan = np.isnan(po)
    return {"bullish": np.where(nan, NAN, bull.astype(float)), "bearish": np.where(nan, NAN, bear.astype(float))}


@register("breakout", "Range breakout levels", "price_action", {"length": L(20)}, ("high", "low", "up", "down"),
          "high/low = highest high / lowest low of the previous n candles (excluding the current one); up/down = close beyond them", lambda p: p["length"] + 1)
def _breakout(df, ctx, length):
    h, l, c = _hlc(df)
    hh, ll = shift(rolling_max(h, length), 1), shift(rolling_min(l, length), 1)
    return {"high": hh, "low": ll, "up": np.where(np.isnan(hh), NAN, (c > hh).astype(float)), "down": np.where(np.isnan(ll), NAN, (c < ll).astype(float))}


@register("swing", "Swing highs/lows (support & resistance)", "price_action", {"left": P(3, 1, 50), "right": P(3, 1, 50)}, ("high", "low"),
          "A pivot high is the highest high with `left` lower highs before and `right` lower highs after; it is only reported `right` bars later "
          "(when confirmed) and then held as the latest resistance/support level", lambda p: p["left"] + p["right"] + 1)
def _swing(df, ctx, left, right):
    h, l, _ = _hlc(df)
    n = len(h)
    hi, lo = np.full(n, NAN), np.full(n, NAN)
    w = left + right + 1
    if n >= w:
        win_h, win_l = sliding_window_view(h, w), sliding_window_view(l, w)
        ph = win_h[:, left] == win_h.max(axis=1)
        ph &= (win_h[:, :left] < win_h[:, [left]]).all(axis=1) if left else True
        pl = win_l[:, left] == win_l.min(axis=1)
        pl &= (win_l[:, :left] > win_l[:, [left]]).all(axis=1) if left else True
        # window k covers rows k..k+w-1; the pivot row is k+left; it is confirmed at row k+w-1
        hi[w - 1:] = np.where(ph, win_h[:, left], NAN)
        lo[w - 1:] = np.where(pl, win_l[:, left], NAN)
    return {"high": pd.Series(hi).ffill().to_numpy(), "low": pd.Series(lo).ffill().to_numpy()}


# ───────────────────────── public API ─────────────────────────
class IndicatorError(ValueError):
    pass


def resolve_params(indicator_id: str, params: dict[str, Any] | None) -> dict[str, Any]:
    ind = REGISTRY.get(indicator_id)
    if ind is None:
        raise IndicatorError(f"unknown indicator {indicator_id!r}")
    given = dict(params or {})
    unknown = set(given) - set(ind.params)
    if unknown:
        raise IndicatorError(f"{indicator_id}: unsupported parameter(s) {sorted(unknown)}")
    out: dict[str, Any] = {}
    for name, spec in ind.params.items():
        value = given.get(name, spec.default)
        if spec.kind == "choice":
            if value not in spec.choices:
                raise IndicatorError(f"{indicator_id}.{name} must be one of {list(spec.choices)}")
        elif spec.kind == "text":
            value = str(value)
        else:
            try:
                value = int(value) if spec.kind == "int" and float(value) == int(float(value)) else float(value)
            except (TypeError, ValueError):
                raise IndicatorError(f"{indicator_id}.{name} must be numeric") from None
            if spec.kind == "int" and not isinstance(value, int):
                raise IndicatorError(f"{indicator_id}.{name} must be an integer")
            if (spec.low is not None and value < spec.low) or (spec.high is not None and value > spec.high):
                raise IndicatorError(f"{indicator_id}.{name}={value} outside [{spec.low}, {spec.high}]")
        out[name] = value
    if indicator_id in ("macd",) and out["fast"] >= out["slow"]:
        raise IndicatorError("macd.fast must be smaller than macd.slow")
    return out


def warmup_bars(indicator_id: str, params: dict[str, Any] | None = None) -> int:
    ind = REGISTRY[indicator_id]
    return int(ind.warmup(resolve_params(indicator_id, params)))


def compute(indicator_id: str, df: pd.DataFrame, params: dict[str, Any] | None, ctx: Ctx) -> dict[str, np.ndarray]:
    """Outputs for every row of ``df``; rows inside the warm-up window are NaN. Never mutates ``df``."""
    resolved = resolve_params(indicator_id, params)
    if df.empty:
        return {o: np.array([]) for o in REGISTRY[indicator_id].outputs}
    out = REGISTRY[indicator_id].fn(df, ctx, **resolved)
    clean: dict[str, np.ndarray] = {}
    for key, arr in out.items():
        arr = np.asarray(arr, dtype=float).copy()
        arr[~np.isfinite(arr)] = NAN
        clean[key] = arr
    return clean


def catalog() -> list[dict[str, Any]]:
    return [{"id": i.id, "name": i.name, "category": i.category, "outputs": list(i.outputs), "formula": i.formula, "notes": i.notes, "uses_source": i.uses_source,
             "params": {k: {"default": p.default, "min": p.low, "max": p.high, "kind": p.kind, "choices": list(p.choices)} for k, p in i.params.items()},
             "minimum_bars_default": warmup_bars(i.id, {})} for i in REGISTRY.values()]


# Additional indicators register into REGISTRY through the same API (kept in their own module for size).
from app.services import indicators_extra, indicators_more  # noqa: E402,F401
