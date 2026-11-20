"""Indicator library 1.1.0: moving-average variants, momentum oscillators, volatility and channels.

Registered into the same REGISTRY as ``intraday_indicators`` through the same ``register`` API, so the scanner,
backtester and Builder see one catalogue. Every indicator is causal: the value at bar t uses bars <= t only, rows
inside the warm-up window are NaN, and nothing here is ever a model output. Statistical transforms use float
internally; prices are quantised to Decimal only at the display/storage boundary (see AGENTS.md).
"""
from __future__ import annotations

import math

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

from app.services.intraday_indicators import (
    L, LEN, LEN14, NAN, P, _div, _hlc, _roll_apply, _rsi, ema, register, rma, rolling_max, rolling_min,
    rolling_sum, shift, sma, src, stdev, true_range,
)


def _roc(x: np.ndarray, n: int) -> np.ndarray:
    return 100 * (_div(x, shift(x, n)) - 1)


def _linreg(y: np.ndarray, n: int, offset: int = 0) -> tuple[np.ndarray, np.ndarray]:
    """Least-squares line over the last n values: (value at ``offset`` bars before the end, slope per bar)."""
    xs = np.arange(n, dtype=float)
    xm = (n - 1) / 2
    denom = ((xs - xm) ** 2).sum()
    centred = xs - xm

    def slope_of(w: np.ndarray) -> np.ndarray:
        return (w * centred).sum(axis=1) / denom

    slope = _roll_apply(y, n, slope_of)
    mean = sma(y, n)
    return mean + slope * ((n - 1 - offset) - xm), slope


# ───────────────────────── batch 1: moving averages and transforms ─────────────────────────
@register("dema", "Double Exponential Moving Average", "trend", LEN, ("value",), "2·EMA(x,n) − EMA(EMA(x,n),n)",
          lambda p: 2 * p["length"] - 1, True)
def _dema(df, ctx, length, source="close"):
    e = ema(src(df, source), length)
    return {"value": 2 * e - ema(e, length)}


@register("tema", "Triple Exponential Moving Average", "trend", LEN, ("value",), "3·EMA1 − 3·EMA2 + EMA3",
          lambda p: 3 * p["length"] - 2, True)
def _tema(df, ctx, length, source="close"):
    e1 = ema(src(df, source), length)
    e2 = ema(e1, length)
    return {"value": 3 * e1 - 3 * e2 + ema(e2, length)}


@register("tma", "Triangular Moving Average", "trend", LEN, ("value",), "SMA(SMA(x, ⌈n/2⌉), ⌊n/2⌋+1)",
          lambda p: p["length"], True)
def _tma(df, ctx, length, source="close"):
    return {"value": sma(sma(src(df, source), math.ceil(length / 2)), length // 2 + 1)}


@register("kama", "Kaufman Adaptive Moving Average", "trend", {"length": L(10), "fast": L(2), "slow": L(30)}, ("value",),
          "KAMA_t = KAMA_{t−1} + sc·(x − KAMA_{t−1}); sc = (ER·(2/(fast+1) − 2/(slow+1)) + 2/(slow+1))², ER = |x_t − x_{t−n}| / Σ|Δx|",
          lambda p: p["length"], True)
def _kama(df, ctx, length, fast, slow, source="close"):
    x = src(df, source)
    out = np.full(len(x), NAN)
    if len(x) <= length:
        return {"value": out}
    fast_sc, slow_sc = 2 / (fast + 1), 2 / (slow + 1)
    volatility = rolling_sum(np.abs(np.diff(x, prepend=NAN)), length)
    er = np.full(len(x), NAN)
    er[length:] = _div(np.abs(x[length:] - x[:-length]), volatility[length:])
    sc = (er * (fast_sc - slow_sc) + slow_sc) ** 2
    out[length - 1] = x[length - 1]
    for i in range(length, len(x)):
        out[i] = out[i - 1] + sc[i] * (x[i] - out[i - 1])
    return {"value": out}


@register("vidya", "Variable Index Dynamic Average", "trend", {"length": L(14), "cmo_length": L(9)}, ("value",),
          "VIDYA_t = α·|CMO|·x + (1 − α·|CMO|)·VIDYA_{t−1}, α = 2/(length+1)", lambda p: p["cmo_length"] + 1, True)
def _vidya(df, ctx, length, cmo_length, source="close"):
    x = src(df, source)
    out = np.full(len(x), NAN)
    d = np.diff(x, prepend=NAN)
    su = rolling_sum(np.where(d > 0, d, 0.0) + 0 * d, cmo_length)
    sd = rolling_sum(np.where(d < 0, -d, 0.0) + 0 * d, cmo_length)
    k = np.abs(_div(su - sd, su + sd))
    alpha = 2 / (length + 1)
    if len(x) <= cmo_length:
        return {"value": out}
    out[cmo_length] = x[cmo_length]
    for i in range(cmo_length + 1, len(x)):
        a = alpha * k[i] if not math.isnan(k[i]) else 0.0
        out[i] = a * x[i] + (1 - a) * out[i - 1]
    return {"value": out}


@register("zlema", "Zero-Lag EMA", "trend", LEN, ("value",), "EMA(x + (x − x_{t−lag}), n), lag = ⌊(n−1)/2⌋",
          lambda p: (p["length"] - 1) // 2 + p["length"], True)
def _zlema(df, ctx, length, source="close"):
    x = src(df, source)
    lag = (length - 1) // 2
    return {"value": ema(x + (x - shift(x, lag)), length)}


@register("mcginley", "McGinley Dynamic", "trend", LEN, ("value",),
          "MD_t = MD_{t−1} + (x − MD_{t−1}) / (0.6·n·(x/MD_{t−1})⁴), seeded with the first SMA", lambda p: p["length"], True)
def _mcginley(df, ctx, length, source="close"):
    x = src(df, source)
    out = np.full(len(x), NAN)
    if len(x) < length:
        return {"value": out}
    out[length - 1] = x[:length].mean()
    for i in range(length, len(x)):
        prev = out[i - 1]
        out[i] = prev + (x[i] - prev) / (0.6 * length * (x[i] / prev) ** 4) if prev > 0 and x[i] > 0 else NAN
    return {"value": out}


@register("alma", "Arnaud Legoux Moving Average", "trend",
          {"length": L(9), "offset": P(0.85, 0, 1, "float"), "sigma": P(6.0, 0.1, 50, "float")}, ("value",),
          "Σ w_i·x_i with Gaussian weights w_i = exp(−(i − offset·(n−1))² / (2·(n/sigma)²)), normalised", lambda p: p["length"], True)
def _alma(df, ctx, length, offset, sigma, source="close"):
    m, s = offset * (length - 1), length / sigma
    w = np.exp(-((np.arange(length) - m) ** 2) / (2 * s * s))
    w /= w.sum()
    return {"value": _roll_apply(src(df, source), length, lambda win: win @ w)}


@register("lsma", "Least Squares Moving Average (Linear Regression)", "trend", {"length": L(25, 2), "offset": L(0, 0, 100)},
          ("value", "slope"), "end point of the least-squares line over n bars (offset bars back); slope per bar",
          lambda p: p["length"], True)
def _lsma(df, ctx, length, offset, source="close"):
    value, slope = _linreg(src(df, source), length, offset)
    return {"value": value, "slope": slope}


@register("t3", "Tillson T3", "trend", {"length": L(5), "volume_factor": P(0.7, 0, 1, "float")}, ("value",),
          "c1·e6 + c2·e5 + c3·e4 + c4·e3 over six chained EMAs, v = volume_factor", lambda p: 6 * p["length"] - 5, True)
def _t3(df, ctx, length, volume_factor, source="close"):
    v = volume_factor
    e = ema(src(df, source), length)
    chain = [e]
    for _ in range(5):
        chain.append(ema(chain[-1], length))
    c1, c2 = -(v ** 3), 3 * v ** 2 + 3 * v ** 3
    c3, c4 = -6 * v ** 2 - 3 * v - 3 * v ** 3, 1 + 3 * v + v ** 3 + 3 * v ** 2
    return {"value": c1 * chain[5] + c2 * chain[4] + c3 * chain[3] + c4 * chain[2]}


@register("heikin_ashi", "Heikin-Ashi Candles", "trend", {}, ("open", "high", "low", "close"),
          "close=(o+h+l+c)/4; open_t=(open_{t−1}+close_{t−1})/2; high=max(h,open,close); low=min(l,open,close)", lambda p: 1)
def _heikin(df, ctx, **_):
    o, h, l, c = (df[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    ha_c = (o + h + l + c) / 4
    ha_o = np.empty(len(c))
    if len(c):
        ha_o[0] = (o[0] + c[0]) / 2
        for i in range(1, len(c)):
            ha_o[i] = (ha_o[i - 1] + ha_c[i - 1]) / 2
    return {"open": ha_o, "high": np.maximum(h, np.maximum(ha_o, ha_c)), "low": np.minimum(l, np.minimum(ha_o, ha_c)), "close": ha_c}


@register("median", "Rolling Median", "trend", LEN, ("value",), "median of source over n", lambda p: p["length"], True)
def _median(df, ctx, length, source="close"):
    return {"value": _roll_apply(src(df, source), length, lambda w: np.median(w, axis=1))}


# ───────────────────────── batch 2: momentum ─────────────────────────
@register("ao", "Awesome Oscillator", "momentum", {"fast": L(5), "slow": L(34)}, ("value",), "SMA(hl2, fast) − SMA(hl2, slow)",
          lambda p: p["slow"])
def _ao(df, ctx, fast, slow):
    hl2 = src(df, "hl2")
    return {"value": sma(hl2, fast) - sma(hl2, slow)}


@register("ac", "Accelerator Oscillator", "momentum", {"fast": L(5), "slow": L(34), "smooth": L(5)}, ("value",),
          "AO − SMA(AO, smooth)", lambda p: p["slow"] + p["smooth"] - 1)
def _ac(df, ctx, fast, slow, smooth):
    hl2 = src(df, "hl2")
    ao = sma(hl2, fast) - sma(hl2, slow)
    return {"value": ao - sma(ao, smooth)}


@register("uo", "Ultimate Oscillator", "momentum", {"fast": L(7), "middle": L(14), "slow": L(28)}, ("value",),
          "100·(4·A_fast + 2·A_mid + A_slow)/7, A_k = Σ(close − min(low, prev close)) / Σ TR over k",
          lambda p: p["slow"] + 1)
def _uo(df, ctx, fast, middle, slow):
    h, l, c = _hlc(df)
    pc = shift(c, 1)
    low_ref = np.minimum(l, pc)
    bp = c - low_ref
    tr = np.maximum(h, pc) - low_ref

    def avg(n: int) -> np.ndarray:
        return _div(rolling_sum(bp, n), rolling_sum(tr, n))

    return {"value": 100 * (4 * avg(fast) + 2 * avg(middle) + avg(slow)) / 7}


@register("elder_ray", "Elder Ray (Bull/Bear Power)", "momentum", {"length": L(13)}, ("bull", "bear"),
          "bull = high − EMA(close, n); bear = low − EMA(close, n)", lambda p: p["length"])
def _elder(df, ctx, length):
    e = ema(df["close"].to_numpy(float), length)
    return {"bull": df["high"].to_numpy(float) - e, "bear": df["low"].to_numpy(float) - e}


@register("fisher", "Fisher Transform", "momentum", {"length": L(9)}, ("fisher", "trigger"),
          "value = 0.66·((hl2−LL)/(HH−LL) − 0.5) + 0.67·value_{t−1}, clamped ±0.999; fisher = 0.5·ln((1+value)/(1−value)) + 0.5·fisher_{t−1}; trigger = fisher_{t−1}",
          lambda p: p["length"] + 1)
def _fisher(df, ctx, length):
    hl2 = src(df, "hl2")
    hh, ll = rolling_max(hl2, length), rolling_min(hl2, length)
    n = len(hl2)
    fish, trig = np.full(n, NAN), np.full(n, NAN)
    value = prev_fish = 0.0
    for i in range(length - 1, n):
        span = max(hh[i] - ll[i], 0.001)
        value = 0.66 * ((hl2[i] - ll[i]) / span - 0.5) + 0.67 * value
        value = min(0.999, max(-0.999, value))
        cur = 0.5 * math.log((1 + value) / (1 - value)) + 0.5 * prev_fish
        fish[i], trig[i] = cur, prev_fish if i > length - 1 else NAN
        prev_fish = cur
    return {"fisher": fish, "trigger": trig}


@register("coppock", "Coppock Curve", "momentum", {"long_roc": L(14), "short_roc": L(11), "wma_length": L(10)}, ("value",),
          "WMA(ROC(long) + ROC(short), wma_length)", lambda p: max(p["long_roc"], p["short_roc"]) + p["wma_length"], True)
def _coppock(df, ctx, long_roc, short_roc, wma_length, source="close"):
    from app.services.intraday_indicators import wma
    x = src(df, source)
    return {"value": wma(_roc(x, long_roc) + _roc(x, short_roc), wma_length)}


@register("kst", "Know Sure Thing", "momentum",
          {"roc1": L(10), "roc2": L(15), "roc3": L(20), "roc4": L(30), "sma1": L(10), "sma2": L(10), "sma3": L(10), "sma4": L(15), "signal": L(9)},
          ("kst", "signal"), "KST = 1·SMA(ROC1) + 2·SMA(ROC2) + 3·SMA(ROC3) + 4·SMA(ROC4); signal = SMA(KST)",
          lambda p: max(p["roc1"] + p["sma1"], p["roc2"] + p["sma2"], p["roc3"] + p["sma3"], p["roc4"] + p["sma4"]) + p["signal"] - 1, True)
def _kst(df, ctx, roc1, roc2, roc3, roc4, sma1, sma2, sma3, sma4, signal, source="close"):
    x = src(df, source)
    k = sma(_roc(x, roc1), sma1) + 2 * sma(_roc(x, roc2), sma2) + 3 * sma(_roc(x, roc3), sma3) + 4 * sma(_roc(x, roc4), sma4)
    return {"kst": k, "signal": sma(k, signal)}


@register("tsi", "True Strength Index", "momentum", {"long": L(25), "short": L(13), "signal": L(13)}, ("tsi", "signal"),
          "100·EMA(EMA(Δx, long), short) / EMA(EMA(|Δx|, long), short); signal = EMA(TSI)",
          lambda p: p["long"] + p["short"] + p["signal"] - 1, True)
def _tsi(df, ctx, long, short, signal, source="close"):
    m = np.diff(src(df, source), prepend=NAN)
    value = 100 * _div(ema(ema(m, long), short), ema(ema(np.abs(m), long), short))
    return {"tsi": value, "signal": ema(value, signal)}


def _ppo_like(x: np.ndarray, fast: int, slow: int, signal: int) -> dict[str, np.ndarray]:
    slow_ma = ema(x, slow)
    line = 100 * _div(ema(x, fast) - slow_ma, slow_ma)
    sig = ema(line, signal)
    return {"ppo": line, "signal": sig, "hist": line - sig}


@register("ppo", "Percentage Price Oscillator", "momentum", {"fast": L(12), "slow": L(26), "signal": L(9)}, ("ppo", "signal", "hist"),
          "100·(EMA(fast) − EMA(slow)) / EMA(slow); signal = EMA(PPO)", lambda p: p["slow"] + p["signal"] - 1, True)
def _ppo(df, ctx, fast, slow, signal, source="close"):
    return _ppo_like(src(df, source), fast, slow, signal)


@register("pvo", "Percentage Volume Oscillator", "volume", {"fast": L(12), "slow": L(26), "signal": L(9)}, ("pvo", "signal", "hist"),
          "PPO of volume", lambda p: p["slow"] + p["signal"] - 1)
def _pvo(df, ctx, fast, slow, signal):
    out = _ppo_like(df["volume"].to_numpy(float), fast, slow, signal)
    return {"pvo": out["ppo"], "signal": out["signal"], "hist": out["hist"]}


@register("smi", "Stochastic Momentum Index", "momentum", {"length": L(10), "smooth": L(3), "signal": L(3)}, ("smi", "signal"),
          "200·EMA(EMA(close − (HH+LL)/2)) / EMA(EMA(HH − LL)); signal = EMA(SMI)",
          lambda p: p["length"] + 2 * (p["smooth"] - 1) + p["signal"] - 1)
def _smi(df, ctx, length, smooth, signal):
    h, l, c = _hlc(df)
    hh, ll = rolling_max(h, length), rolling_min(l, length)
    num = ema(ema(c - (hh + ll) / 2, smooth), smooth)
    den = ema(ema(hh - ll, smooth), smooth)
    value = 200 * _div(num, den)
    return {"smi": value, "signal": ema(value, signal)}


@register("rvi", "Relative Vigor Index", "momentum", {"length": L(10)}, ("rvi", "signal"),
          "SMA(sw4(close−open), n) / SMA(sw4(high−low), n), sw4 = (a + 2·a₋₁ + 2·a₋₂ + a₋₃)/6; signal = sw4(RVI)",
          lambda p: p["length"] + 3 + 3)
def _rvi(df, ctx, length):
    o, h, l, c = (df[k].to_numpy(float) for k in ("open", "high", "low", "close"))

    def sw4(a: np.ndarray) -> np.ndarray:
        return (a + 2 * shift(a, 1) + 2 * shift(a, 2) + shift(a, 3)) / 6

    value = _div(sma(sw4(c - o), length), sma(sw4(h - l), length))
    return {"rvi": value, "signal": sw4(value)}


def _percent_rank(x: np.ndarray, n: int) -> np.ndarray:
    """Share (0-100) of the previous n values that are <= the current one; the current bar is excluded."""
    out = np.full(len(x), NAN)
    if len(x) > n:
        win = sliding_window_view(x[:-1], n)
        out[n:] = 100 * (win <= x[n:, None]).sum(axis=1) / n
    return out


@register("connors_rsi", "Connors RSI", "momentum", {"rsi_length": L(3), "streak_length": L(2), "rank_length": L(100)}, ("value",),
          "(RSI(close, 3) + RSI(up/down streak, 2) + PercentRank(ROC(1), 100)) / 3",
          lambda p: max(p["rsi_length"] + 1, p["streak_length"] + 2, p["rank_length"] + 2), True)
def _connors(df, ctx, rsi_length, streak_length, rank_length, source="close"):
    x = src(df, source)
    streak = np.zeros(len(x))
    for i in range(1, len(x)):
        step = x[i] - x[i - 1]
        streak[i] = (max(streak[i - 1], 0) + 1 if step > 0 else min(streak[i - 1], 0) - 1 if step < 0 else 0)
    streak[0] = NAN
    return {"value": (_rsi(x, rsi_length) + _rsi(streak, streak_length) + _percent_rank(_roc(x, 1), rank_length)) / 3}


@register("dpo", "Detrended Price Oscillator", "momentum", LEN, ("value",), "close_{t−(⌊n/2⌋+1)} − SMA(close, n)",
          lambda p: p["length"], True)
def _dpo(df, ctx, length, source="close"):
    x = src(df, source)
    return {"value": shift(x, length // 2 + 1) - sma(x, length)}


@register("vortex", "Vortex Indicator", "trend", LEN14, ("plus", "minus"),
          "VI± = Σ|high − low₋₁| (or |low − high₋₁|) / Σ TR over n", lambda p: p["length"] + 1)
def _vortex(df, ctx, length):
    h, l, c = _hlc(df)
    tr = rolling_sum(true_range(h, l, c), length)
    return {"plus": _div(rolling_sum(np.abs(h - shift(l, 1)), length), tr), "minus": _div(rolling_sum(np.abs(l - shift(h, 1)), length), tr)}


@register("cmo", "Chande Momentum Oscillator", "momentum", {"length": L(9)}, ("value",), "100·(Σ up − Σ down)/(Σ up + Σ down) over n",
          lambda p: p["length"] + 1, True)
def _cmo(df, ctx, length, source="close"):
    d = np.diff(src(df, source), prepend=NAN)
    su, sd = rolling_sum(np.where(d > 0, d, 0.0) + 0 * d, length), rolling_sum(np.where(d < 0, -d, 0.0) + 0 * d, length)
    return {"value": 100 * _div(su - sd, su + sd)}


@register("bop", "Balance of Power", "momentum", {}, ("value",), "(close − open)/(high − low)", lambda p: 1)
def _bop(df, ctx, **_):
    o, h, l, c = (df[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    return {"value": _div(c - o, h - l)}


@register("squeeze", "Squeeze Momentum", "momentum", {"length": L(20), "bb_mult": P(2.0, 0.1, 10, "float"), "kc_mult": P(1.5, 0.1, 10, "float")},
          ("momentum", "squeeze_on", "squeeze_off"),
          "squeeze_on: Bollinger bands inside Keltner (SMA(TR)); momentum = linreg(close − avg(avg(HH,LL), SMA(close)), n)",
          lambda p: 2 * p["length"] - 1)
def _squeeze(df, ctx, length, bb_mult, kc_mult):
    h, l, c = _hlc(df)
    basis = sma(c, length)
    dev = bb_mult * stdev(c, length)
    rng = sma(true_range(h, l, c), length) * kc_mult
    upper_bb, lower_bb, upper_kc, lower_kc = basis + dev, basis - dev, basis + rng, basis - rng
    on = np.where(np.isnan(upper_kc) | np.isnan(upper_bb), NAN, ((lower_bb > lower_kc) & (upper_bb < upper_kc)).astype(float))
    off = np.where(np.isnan(on), NAN, ((lower_bb < lower_kc) & (upper_bb > upper_kc)).astype(float))
    mid = (rolling_max(h, length) + rolling_min(l, length)) / 2
    momentum, _slope = _linreg(c - (mid + basis) / 2, length)
    return {"momentum": momentum, "squeeze_on": on, "squeeze_off": off}


@register("wavetrend", "WaveTrend Oscillator", "momentum", {"channel": L(10), "average": L(21), "smooth": L(4)}, ("wt1", "wt2"),
          "ci = (ap − EMA(ap))/(0.015·EMA(|ap − EMA(ap)|)), ap = hlc3; WT1 = EMA(ci, average); WT2 = SMA(WT1, smooth)",
          lambda p: 2 * p["channel"] + p["average"] + p["smooth"] - 3)
def _wavetrend(df, ctx, channel, average, smooth):
    ap = src(df, "hlc3")
    esa = ema(ap, channel)
    d = ema(np.abs(ap - esa), channel)
    wt1 = ema(_div(ap - esa, 0.015 * d), average)
    return {"wt1": wt1, "wt2": sma(wt1, smooth)}


@register("laguerre_rsi", "Laguerre RSI", "momentum", {"gamma": P(0.5, 0.01, 0.99, "float")}, ("value",),
          "RSI on a four-element Laguerre filter, range 0–100", lambda p: 20, True)
def _laguerre(df, ctx, gamma, source="close"):
    x = src(df, source)
    out = np.full(len(x), NAN)
    l0 = l1 = l2 = l3 = x[0] if len(x) else 0.0
    for i in range(len(x)):
        p0, p1, p2 = l0, l1, l2
        l0 = (1 - gamma) * x[i] + gamma * p0
        l1 = -gamma * l0 + p0 + gamma * l1
        l2 = -gamma * l1 + p1 + gamma * l2
        l3 = -gamma * l2 + p2 + gamma * l3
        pairs = ((l0, l1), (l1, l2), (l2, l3))
        up = sum(max(a - b, 0.0) for a, b in pairs)
        down = sum(max(b - a, 0.0) for a, b in pairs)
        out[i] = 100 * up / (up + down) if up + down > 0 else 50.0
    out[:19] = NAN  # the filter needs time to settle; match the declared warm-up
    return {"value": out}


@register("williams_fractals", "Williams Fractals", "trend", {"length": L(2, 1, 20)}, ("up", "down"),
          "high (low) that is the strict max (min) of 2n+1 bars; reported n bars later, when the pattern is complete (no look-ahead)",
          lambda p: 2 * p["length"] + 1)
def _fractals(df, ctx, length):
    h, l, _ = _hlc(df)
    n, w = len(h), 2 * length + 1
    up, down = np.full(n, NAN), np.full(n, NAN)
    if n >= w:
        hw, lw = sliding_window_view(h, w), sliding_window_view(l, w)
        centre_h, centre_l = hw[:, length], lw[:, length]
        up[w - 1:] = ((centre_h == hw.max(axis=1)) & ((hw == centre_h[:, None]).sum(axis=1) == 1)).astype(float)
        down[w - 1:] = ((centre_l == lw.min(axis=1)) & ((lw == centre_l[:, None]).sum(axis=1) == 1)).astype(float)
    return {"up": up, "down": down}


# ───────────────────────── batch 3: volatility and channels ─────────────────────────
@register("bb_percent_b", "Bollinger %B", "volatility", {"length": L(20), "multiplier": P(2.0, 0.1, 10, "float")}, ("value",),
          "(source − lower)/(upper − lower)", lambda p: p["length"], True)
def _pctb(df, ctx, length, multiplier, source="close"):
    x = src(df, source)
    mid, dev = sma(x, length), multiplier * stdev(x, length)
    return {"value": _div(x - (mid - dev), 2 * dev)}


@register("bb_width", "Bollinger Bandwidth", "volatility", {"length": L(20), "multiplier": P(2.0, 0.1, 10, "float")}, ("value",),
          "(upper − lower)/middle", lambda p: p["length"], True)
def _bbw(df, ctx, length, multiplier, source="close"):
    x = src(df, source)
    return {"value": _div(2 * multiplier * stdev(x, length), sma(x, length))}


@register("chandelier", "Chandelier Exit", "volatility", {"length": L(22), "multiplier": P(3.0, 0.1, 20, "float")}, ("long_stop", "short_stop"),
          "long = HH(high,n) − mult·ATR(n); short = LL(low,n) + mult·ATR(n)", lambda p: p["length"])
def _chandelier(df, ctx, length, multiplier):
    h, l, c = _hlc(df)
    atr = rma(true_range(h, l, c), length)
    return {"long_stop": rolling_max(h, length) - multiplier * atr, "short_stop": rolling_min(l, length) + multiplier * atr}


@register("ulcer", "Ulcer Index", "volatility", LEN14, ("value",), "√SMA(((close − HH(close,n))/HH(close,n)·100)², n)",
          lambda p: 2 * p["length"] - 1, True)
def _ulcer(df, ctx, length, source="close"):
    x = src(df, source)
    drawdown = 100 * _div(x - rolling_max(x, length), rolling_max(x, length))
    return {"value": np.sqrt(sma(drawdown ** 2, length))}


@register("chaikin_vol", "Chaikin Volatility", "volatility", {"length": L(10), "roc_length": L(10)}, ("value",),
          "100·(EMA(high−low, n) − EMA(high−low, n)_{t−r}) / EMA(high−low, n)_{t−r}", lambda p: p["length"] + p["roc_length"])
def _chaikin_vol(df, ctx, length, roc_length):
    e = ema(df["high"].to_numpy(float) - df["low"].to_numpy(float), length)
    return {"value": 100 * _div(e - shift(e, roc_length), shift(e, roc_length))}


@register("mass_index", "Mass Index", "volatility", {"ema_length": L(9), "sum_length": L(25)}, ("value",),
          "Σ over m of EMA(high−low)/EMA(EMA(high−low))", lambda p: 2 * (p["ema_length"] - 1) + p["sum_length"])
def _mass(df, ctx, ema_length, sum_length):
    e1 = ema(df["high"].to_numpy(float) - df["low"].to_numpy(float), ema_length)
    return {"value": rolling_sum(_div(e1, ema(e1, ema_length)), sum_length)}


@register("envelope", "Moving Average Envelope", "volatility", {"length": L(20), "percent": P(10.0, 0.01, 100, "float")},
          ("upper", "middle", "lower"), "SMA(source, n) ± percent%", lambda p: p["length"], True)
def _envelope(df, ctx, length, percent, source="close"):
    mid = sma(src(df, source), length)
    return {"upper": mid * (1 + percent / 100), "middle": mid, "lower": mid * (1 - percent / 100)}


@register("natr", "Normalised ATR", "volatility", LEN14, ("value",), "100·ATR(n)/close", lambda p: p["length"])
def _natr(df, ctx, length):
    h, l, c = _hlc(df)
    return {"value": 100 * _div(rma(true_range(h, l, c), length), c)}


@register("true_range", "True Range", "volatility", {}, ("value",), "max(H−L, |H−C₋₁|, |L−C₋₁|)", lambda p: 1)
def _tr(df, ctx, **_):
    h, l, c = _hlc(df)
    return {"value": true_range(h, l, c)}


@register("rvi_volatility", "Relative Volatility Index", "volatility", {"stdev_length": L(10), "length": L(14)}, ("value",),
          "100·EMA(σ on up-closes)/(EMA(σ on up-closes) + EMA(σ on down-closes))", lambda p: p["stdev_length"] + p["length"] - 1, True)
def _rvi_vol(df, ctx, stdev_length, length, source="close"):
    x = src(df, source)
    sd = stdev(x, stdev_length)
    d = np.diff(x, prepend=NAN)
    valid = ~np.isnan(sd) & ~np.isnan(d)
    up = np.where(valid, np.where(d > 0, sd, 0.0), NAN)
    down = np.where(valid, np.where(d <= 0, sd, 0.0), NAN)
    u, dn = ema(up, length), ema(down, length)
    return {"value": 100 * _div(u, u + dn)}


@register("chande_kroll", "Chande Kroll Stop", "volatility", {"length": L(10), "multiplier": P(1.0, 0.1, 20, "float"), "stop_length": L(9)},
          ("stop_long", "stop_short"), "stop_short = HH(HH(high,p) − x·ATR, q); stop_long = LL(LL(low,p) + x·ATR, q)",
          lambda p: p["length"] + p["stop_length"] - 1)
def _chande_kroll(df, ctx, length, multiplier, stop_length):
    h, l, c = _hlc(df)
    atr = rma(true_range(h, l, c), length)
    first_high = rolling_max(h, length) - multiplier * atr
    first_low = rolling_min(l, length) + multiplier * atr
    return {"stop_long": rolling_min(first_low, stop_length), "stop_short": rolling_max(first_high, stop_length)}
