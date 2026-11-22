"""Nexus v2 Phase 7, Step 1: the seven lenses. Each returns {score 0-100 | None, direction positive|neutral|negative|unknown, facts[], coverage}. The CHART lens is built from pure, individually tested
detectors over COMPLETED candles (split-adjusted by the caller; tick data is never used). It reuses technical_engine's indicators, swing points and resampling; the Phase 1 headroom is passed in from the v2
level card. Score = (positive detectors firing - negative detectors firing) / applicable detectors, mapped from [-1, 1] to 0-100. Unknown inputs make a detector inapplicable, never false.
Decision support, not investment advice."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd

from app.services import technical_engine as te

LENSES = ("chart", "financial_trends", "fundamentals", "growth", "valuation", "news_events", "market_regime")


@dataclass
class ChartDetectorConfig:
    # EMA stack spans per frame (daily 20/50/200; weekly and monthly frames use proportionally shorter spans because 200 weeks of history do not exist for most stocks)
    spans: dict[str, tuple[int, int, int]] = field(default_factory=lambda: {"D": (20, 50, 200), "W": (10, 20, 40), "M": (6, 12, 24)})
    pivot_width: dict[str, int] = field(default_factory=lambda: {"D": 5, "W": 3, "M": 2})
    adx_min: float = 20.0
    rs_windows: tuple[int, int] = (20, 60)
    min_pullbacks: int = 3
    atr_pct_low_third: float = 1 / 3
    breakout_atr: float = 0.5
    breakout_volume_mult: float = 1.5
    breakout_lookback: int = 60
    retest_atr: float = 1.0
    retest_closes: int = 2
    flag_rise: float = 0.15
    flag_pole_bars: int = 15
    flag_pullback: tuple[float, float] = (0.25, 0.50)
    accumulation_ratio: float = 1.3
    accumulation_days: int = 50
    delivery_days: int = 60
    distribution_days: int = 4
    distribution_window: int = 25
    distribution_volume_mult: float = 1.5
    extension_atr: float = 3.0
    extension_pct_over_sma30w: float = 0.25
    headroom_min_r: float = 1.5
    rsi_band: tuple[float, float] = (40.0, 80.0)
    min_bars: dict[str, int] = field(default_factory=lambda: {"D": 120, "W": 40, "M": 24})


def _det(did: str, value: bool | None, polarity: str, fact: str) -> dict[str, Any]:
    return {"id": did, "value": value, "polarity": polarity, "fact": fact}


# ───────────────────────── detectors (pure functions; each returns {id, value True|False|None, polarity, fact}) ─────────────────────────
def trend_stack(df: pd.DataFrame, frame: str, cfg: ChartDetectorConfig) -> dict[str, Any]:
    a, b, c = cfg.spans[frame]
    if len(df) < c + 20:
        return _det("trend_stack", None, "+", f"Needs {c + 20} candles for the {a}/{b}/{c} average stack; {len(df)} stored.")
    close = df["close"]
    e1, e2, e3 = te.ema(close, a), te.ema(close, b), te.ema(close, c)
    ok = bool(close.iloc[-1] > e1.iloc[-1] > e2.iloc[-1] > e3.iloc[-1] and e3.iloc[-1] > e3.iloc[-21])
    return _det("trend_stack", ok, "+", f"Close {'above' if ok else 'not stacked above'} the {a}, {b} and {c}-period averages with the {c}-period average {'rising' if e3.iloc[-1] > e3.iloc[-21] else 'not rising'}.")


def market_structure(df: pd.DataFrame, frame: str, cfg: ChartDetectorConfig) -> dict[str, Any]:
    highs, lows = te.swing_points(df, cfg.pivot_width[frame])
    if len(highs) < 2 or len(lows) < 2:
        return _det("market_structure", None, "+", "Fewer than two swing highs and lows are confirmed.")
    ok = bool(highs[-1][1] > highs[-2][1] and lows[-1][1] > lows[-2][1])
    return _det("market_structure", ok, "+", f"Last two swing highs {'rising' if highs[-1][1] > highs[-2][1] else 'not rising'} and lows {'rising' if lows[-1][1] > lows[-2][1] else 'not rising'}.")


def trend_strength(df: pd.DataFrame, frame: str, cfg: ChartDetectorConfig) -> dict[str, Any]:
    if len(df) < 40:
        return _det("trend_strength", None, "+", "Fewer than 40 candles for ADX.")
    up, dn = df["high"].diff(), -df["low"].diff()
    plus = np.where((up > dn) & (up > 0), up, 0.0)
    minus = np.where((dn > up) & (dn > 0), dn, 0.0)
    tr = te.atr(df, 14)
    pdi = 100 * pd.Series(plus, index=df.index).ewm(alpha=1 / 14, adjust=False).mean() / tr
    mdi = 100 * pd.Series(minus, index=df.index).ewm(alpha=1 / 14, adjust=False).mean() / tr
    adx = float(te.adx(df, 14).iloc[-1])
    ok = bool(adx > cfg.adx_min and pdi.iloc[-1] > mdi.iloc[-1])
    return _det("trend_strength", ok, "+", f"ADX {adx:.0f} ({'above' if adx > cfg.adx_min else 'at or below'} {cfg.adx_min:g}), +DI {'above' if pdi.iloc[-1] > mdi.iloc[-1] else 'below'} -DI.")


def relative_strength(df: pd.DataFrame, bench: pd.Series | None, cfg: ChartDetectorConfig, sector_returns: dict[int, float] | None = None) -> dict[str, Any]:
    w1, w2 = cfg.rs_windows
    if bench is None or len(df) <= w2 + 1:
        return _det("relative_strength", None, "+", "No benchmark series or too few candles.")
    b = bench.reindex(df.index, method="ffill").dropna()
    if len(b) <= w2 + 1:
        return _det("relative_strength", None, "+", "Benchmark history is too short.")
    r = lambda s, n: float(s.iloc[-1] / s.iloc[-1 - n] - 1)  # noqa: E731
    ex1, ex2 = r(df["close"], w1) - r(b, w1), r(df["close"], w2) - r(b, w2)
    ok = ex1 > 0 and ex2 > 0
    sect = ""
    if sector_returns:
        s_ok = r(df["close"], w1) > sector_returns.get(w1, -9) and r(df["close"], w2) > sector_returns.get(w2, -9)
        ok = ok and s_ok
        sect = f"; {'ahead of' if s_ok else 'behind'} its industry median"
    else:
        sect = "; industry median unknown"
    return _det("relative_strength", bool(ok), "+", f"Return minus NIFTY 50: {ex1:+.1%} over {w1} bars, {ex2:+.1%} over {w2}{sect}.")


def volatility_contraction(df: pd.DataFrame, frame: str, cfg: ChartDetectorConfig) -> dict[str, Any]:
    if len(df) < 120:
        return _det("volatility_contraction", None, "+", "Fewer than 120 candles for a base.")
    k = cfg.pivot_width[frame]
    highs, lows = te.swing_points(df.iloc[-160:], k)
    depths = []
    for hi in highs:
        nxt = [lo for lo in lows if lo[0] > hi[0]]
        if nxt:
            depths.append((hi[1] - nxt[0][1]) / hi[1])
    depths = depths[-cfg.min_pullbacks:]
    shallower = len(depths) >= cfg.min_pullbacks and all(depths[i] > depths[i + 1] for i in range(len(depths) - 1))
    atr_pct = (te.atr(df, 14) / df["close"]).dropna().iloc[-252:]
    low_third = bool(len(atr_pct) >= 60 and atr_pct.iloc[-1] <= atr_pct.quantile(cfg.atr_pct_low_third))
    return _det("volatility_contraction", bool(shallower and low_third), "+", f"{len(depths)} recent pullbacks ({', '.join(f'{d:.0%}' for d in depths) or 'none'}), {'each shallower' if shallower else 'not progressively shallower'}; volatility {'in' if low_third else 'not in'} the lowest third of its year.")


def _breakout_info(df: pd.DataFrame, cfg: ChartDetectorConfig, look: int = 3) -> tuple[float, int] | None:
    """(broken level, bars ago) of the latest qualifying breakout within the last ``look`` bars, else None."""
    if len(df) < cfg.breakout_lookback + 60:
        return None
    a = te.atr(df, 14)
    vol50 = df["volume"].rolling(50).mean()
    for ago in range(0, look):
        i = len(df) - 1 - ago
        lvl = float(df["high"].iloc[i - cfg.breakout_lookback:i - 3].max())
        if df["close"].iloc[i] > lvl + cfg.breakout_atr * a.iloc[i] and df["volume"].iloc[i] >= cfg.breakout_volume_mult * vol50.iloc[i - 1]:
            return lvl, ago
    return None


def breakout(df: pd.DataFrame, cfg: ChartDetectorConfig) -> dict[str, Any]:
    if len(df) < cfg.breakout_lookback + 60:
        return _det("breakout", None, "+", f"Needs {cfg.breakout_lookback + 60} candles.")
    info = _breakout_info(df, cfg)
    return _det("breakout", info is not None, "+", f"Closed above the {cfg.breakout_lookback}-bar resistance by at least {cfg.breakout_atr:g} ATR on {cfg.breakout_volume_mult:g}x volume {info[1]} bar(s) ago." if info else "No close above resistance on heavy volume in the last 3 candles.")


def breakout_retest(df: pd.DataFrame, cfg: ChartDetectorConfig) -> dict[str, Any]:
    if len(df) < cfg.breakout_lookback + 60:
        return _det("breakout_retest", None, "+", f"Needs {cfg.breakout_lookback + 60} candles.")
    a = te.atr(df, 14)
    for ago in range(4, 21):
        past = df.iloc[: len(df) - ago]
        info = _breakout_info(past, cfg, look=1)
        if info is None:
            continue
        lvl = info[0]
        recent = df["close"].iloc[-cfg.retest_closes:]
        near = bool(abs(df["close"].iloc[-1] - lvl) <= cfg.retest_atr * a.iloc[-1] and (recent >= lvl - 0.5 * a.iloc[-1]).all())
        dipped = bool((df["close"].iloc[-ago:-1] < lvl + cfg.retest_atr * a.iloc[-1]).any())
        return _det("breakout_retest", bool(near and dipped), "+", f"Broke {lvl:.2f} {ago} bars ago; price {'is back within' if near and dipped else 'has not come back within'} {cfg.retest_atr:g} ATR and {'holding' if near else 'not holding'} for {cfg.retest_closes} closes.")
    return _det("breakout_retest", False, "+", "No breakout in the last 20 candles to retest.")


def flag_pattern(df: pd.DataFrame, cfg: ChartDetectorConfig) -> dict[str, Any]:
    if len(df) < 60:
        return _det("flag", None, "+", "Fewer than 60 candles.")
    tail = df.iloc[-45:]
    end = int(np.argmax(tail["high"].values))                          # the pole top is the highest high of the last 45 candles
    lo, hi = float(tail["low"].iloc[max(0, end - cfg.flag_pole_bars):end + 1].min()), float(tail["high"].iloc[end])
    if end >= len(tail) - 2 or lo <= 0 or hi / lo - 1 < cfg.flag_rise:
        return _det("flag", False, "+", f"No rise of {cfg.flag_rise:.0%} within {cfg.flag_pole_bars} bars followed by a pullback in the last 45 candles.")
    pull = (hi - float(tail["close"].iloc[-1])) / (hi - lo)
    pole_vol = float(tail["volume"].iloc[max(0, end - cfg.flag_pole_bars):end + 1].mean())
    flag_vol = float(tail["volume"].iloc[end + 1:].mean()) if end + 1 < len(tail) else float("nan")
    ok = bool(cfg.flag_pullback[0] <= pull <= cfg.flag_pullback[1] and flag_vol < pole_vol)
    return _det("flag", ok, "+", f"Pole of {hi / lo - 1:.0%}, pullback {pull:.0%} of it, volume {'falling' if flag_vol < pole_vol else 'not falling'}.")


def accumulation(df: pd.DataFrame, delivery_pct: pd.Series | None, cfg: ChartDetectorConfig) -> dict[str, Any]:
    n = cfg.accumulation_days
    if len(df) < n + 1:
        return _det("accumulation", None, "+", f"Fewer than {n + 1} candles.")
    if delivery_pct is None or len(delivery_pct.dropna()) < cfg.delivery_days:
        return _det("accumulation", None, "+", "NSE delivery percentage is not stored for this stock: unknown.")
    ch = df["close"].diff().iloc[-n:]
    vol = df["volume"].iloc[-n:]
    up, dn = float(vol[ch > 0].sum()), float(vol[ch < 0].sum())
    ratio = up / dn if dn > 0 else float("inf")
    d = delivery_pct.dropna()
    d_ok = bool(d.iloc[-10:].mean() > d.iloc[-cfg.delivery_days:].mean())
    return _det("accumulation", bool(ratio > cfg.accumulation_ratio and d_ok), "+", f"Up-volume to down-volume {ratio:.2f} over {n} days (needs above {cfg.accumulation_ratio:g}); delivery {'above' if d_ok else 'not above'} its {cfg.delivery_days}-day average.")


def distribution(df: pd.DataFrame, cfg: ChartDetectorConfig) -> dict[str, Any]:
    if len(df) < 60:
        return _det("distribution", None, "-", "Fewer than 60 candles.")
    w = cfg.distribution_window
    vol50 = df["volume"].rolling(50).mean().shift(1)
    tail = df.iloc[-w:]
    heavy_down = int(((tail["close"] < tail["close"].shift(1)) & (tail["volume"] > cfg.distribution_volume_mult * vol50.iloc[-w:])).sum())
    last = df.iloc[-5:]
    prior_high = float(df["high"].iloc[-25:-5].max())
    rng = (last["high"] - last["low"]).replace(0, np.nan)
    reversal = bool(((last["high"] >= prior_high * 0.995) & (last["close"] < last["open"]) & (last["volume"] > 2 * vol50.iloc[-5:]) & ((last["close"] - last["low"]) / rng < 0.3)).any())
    return _det("distribution", bool(heavy_down >= cfg.distribution_days or reversal), "-", f"{heavy_down} heavy-volume down days in {w} sessions (flag at {cfg.distribution_days}); {'a high-volume reversal at resistance' if reversal else 'no high-volume reversal at resistance'}.")


def extension(df: pd.DataFrame, frame: str, horizon: str, cfg: ChartDetectorConfig) -> dict[str, Any]:
    if horizon == "long_term" or frame == "M":
        weekly = te.resample(df, "W") if frame == "D" else df
        if len(weekly) < 40:
            return _det("extension", None, "-", "Fewer than 40 weekly candles for the 30-week average.")
        sma = weekly["close"].rolling(30).mean().iloc[-1]
        over = float(weekly["close"].iloc[-1] / sma - 1)
        return _det("extension", over > cfg.extension_pct_over_sma30w, "-", f"Close is {over:+.0%} versus its 30-week average (flag above +{cfg.extension_pct_over_sma30w:.0%}).")
    if len(df) < 40:
        return _det("extension", None, "-", "Fewer than 40 candles.")
    ema20, a = te.ema(df["close"], 20).iloc[-1], te.atr(df, 14).iloc[-1]
    x = float((df["close"].iloc[-1] - ema20) / a)
    return _det("extension", x > cfg.extension_atr, "-", f"Close is {x:.1f} ATR above the 20-period average (flag above {cfg.extension_atr:g}).")


def headroom(headroom_r: float | None, cfg: ChartDetectorConfig) -> dict[str, Any]:
    if headroom_r is None:
        return _det("headroom", None, "+", "No v2 level card (no valid ladder), so the distance to the next resistance in R is unknown.")
    return _det("headroom", headroom_r >= cfg.headroom_min_r, "+", f"Next resistance is {headroom_r:.1f}R away (needs {cfg.headroom_min_r:g}R).")


def momentum_regime(df: pd.DataFrame, frame: str, cfg: ChartDetectorConfig) -> dict[str, Any]:
    if len(df) < 60:
        return _det("momentum_regime", None, "+", "Fewer than 60 candles.")
    r = te.rsi(df["close"], 14)
    up = bool(df["close"].iloc[-1] > te.ema(df["close"], cfg.spans[frame][1]).iloc[-1])
    held = bool(r.iloc[-10:].between(*cfg.rsi_band).all())
    h = te.macd_hist(df["close"])
    rising = bool(h.iloc[-1] > h.iloc[-4])
    return _det("momentum_regime", bool(up and held and rising), "+", f"RSI {r.iloc[-1]:.0f} ({'held' if held else 'not held'} within {cfg.rsi_band[0]:g}-{cfg.rsi_band[1]:g}); MACD histogram {'rising' if rising else 'not rising'}; {'above' if up else 'below'} the trend average.")


# ───────────────────────── the lens ─────────────────────────
def lens_from_detectors(dets: list[dict[str, Any]], title: str = "chart") -> dict[str, Any]:
    app = [d for d in dets if d["value"] is not None]
    pos = sum(1 for d in app if d["value"] and d["polarity"] == "+")
    neg = sum(1 for d in app if d["value"] and d["polarity"] == "-")
    if not app:
        return {"score": None, "direction": "unknown", "facts": [d["fact"] for d in dets][:6], "coverage": 0.0, "detectors": dets}
    x = (pos - neg) / len(app)
    score = round((x + 1) / 2 * 100, 1)
    direction = "positive" if score >= 60 else "negative" if score <= 40 else "neutral"
    fired = [d["fact"] for d in app if d["value"]]
    return {"score": score, "direction": direction, "facts": (fired or [d["fact"] for d in app])[:6], "coverage": round(len(app) / len(dets), 3), "detectors": dets, "positive": pos, "negative": neg, "applicable": len(app)}


def chart_lens(daily: pd.DataFrame | None, horizon: str, bench_daily: pd.Series | None = None, *, delivery_pct: pd.Series | None = None, sector_returns: dict[int, float] | None = None,
               headroom_r: float | None = None, as_of: pd.Timestamp | None = None, cfg: ChartDetectorConfig | None = None) -> dict[str, Any]:
    """Chart lens for one horizon from COMPLETED candles: daily for swing, weekly for positional, monthly (weekly if under 24 months) for long-term."""
    cfg = cfg or ChartDetectorConfig()
    if daily is None or len(daily) == 0:
        return {"score": None, "direction": "unknown", "facts": ["No stored daily candles."], "coverage": 0.0, "detectors": [], "frame": None}
    frame = {"swing": "D", "positional": "W", "long_term": "M"}[horizon]
    df = daily if frame == "D" else te.resample(daily, frame, as_of)
    if frame == "M" and len(df) < cfg.min_bars["M"]:
        frame, df = "W", te.resample(daily, "W", as_of)
    if len(df) < min(cfg.min_bars[frame], 30):
        return {"score": None, "direction": "unknown", "facts": [f"Only {len(df)} completed {'daily' if frame == 'D' else 'weekly' if frame == 'W' else 'monthly'} candles are stored."], "coverage": 0.0, "detectors": [], "frame": frame}
    b = bench_daily if frame == "D" else (None if bench_daily is None else bench_daily.resample("W-FRI" if frame == "W" else "ME").last().dropna())
    dets = [trend_stack(df, frame, cfg), market_structure(df, frame, cfg), trend_strength(df, frame, cfg), relative_strength(df, b, cfg, sector_returns), volatility_contraction(df, frame, cfg),
            breakout(df, cfg), breakout_retest(df, cfg), flag_pattern(df, cfg), accumulation(df, delivery_pct if frame == "D" else None, cfg), distribution(df, cfg), extension(df, frame, horizon, cfg),
            headroom(headroom_r, cfg), momentum_regime(df, frame, cfg)]
    out = lens_from_detectors(dets)
    out["frame"] = frame
    return out


# ───────────────────────── the other lenses: thin wrappers over what already exists ─────────────────────────
def lens_result(score: float | None, facts: list[str], coverage: float, *, positive_at: float = 60.0, negative_at: float = 40.0) -> dict[str, Any]:
    if score is None:
        return {"score": None, "direction": "unknown", "facts": facts, "coverage": coverage}
    return {"score": round(float(score), 1), "direction": "positive" if score >= positive_at else "negative" if score <= negative_at else "neutral", "facts": facts, "coverage": coverage}


def financial_trends_lens(component: dict[str, Any] | None, t_score: float | None, t_text: str | None) -> dict[str, Any]:
    """The existing score_financial_trends component plus the Phase 6 turning-point score T (average of the two when both exist)."""
    parts, facts = [], []
    if component and component.get("status") == "scored" and component.get("score") is not None:
        parts.append(float(component["score"]))
        facts += [str(f) for f in (component.get("facts") or component.get("reasons") or [])][:3]
    if t_score is not None:
        parts.append(float(t_score))
        facts.append(t_text or f"Turning point score {t_score:.0f}.")
    return lens_result(sum(parts) / len(parts) if parts else None, facts or ["No comparable financial periods are stored."], len(parts) / 2)


def fundamentals_lens(mb: dict[str, Any] | None) -> dict[str, Any]:
    """Phase 6 Q plus the class and the vetoes."""
    if not mb:
        return lens_result(None, ["No multibagger-engine result is stored for this stock yet."], 0.0)
    q = (mb.get("scores") or {}).get("Q")
    facts = [f"Class: {mb['class'].replace('_', ' ')}."] + [str(r) for r in (mb.get("reasons") or [])[:2]]
    return lens_result(q, facts, 1.0 if q is not None else 0.0)


def growth_lens(mb: dict[str, Any] | None) -> dict[str, Any]:
    g = ((mb or {}).get("scores") or {}).get("G")
    return lens_result(g, [f"Growth score {g:.0f} (3-year revenue and profit growth, consistency, margin change, runway)."] if g is not None else ["Growth inputs are too thin to score."], 1.0 if g is not None else 0.0)


def valuation_lens(mb: dict[str, Any] | None) -> dict[str, Any]:
    v = ((mb or {}).get("scores") or {}).get("V")
    return lens_result(v, [f"Valuation score {v:.0f} (PEG, own-history PE, peers)."] if v is not None else ["Valuation inputs are too thin to score."], 1.0 if v is not None else 0.0)


MATERIAL_WORDS = ("fraud", "raid", "default", "downgrade", "auditor", "key management", "resign", "order loss", "lost order", "cancelled order")


def news_events_lens(*, headlines: list[str], events: list[dict[str, Any]], governance: list[dict[str, Any]] | None, contradictory: bool, now: date) -> dict[str, Any]:
    """Gate and context only (no score). ``events`` = upcoming results / ex / record dates; ``headlines`` = recent headline texts for the stock; ``governance`` = governance_events (None = unknown)."""
    material = [h for h in headlines if any(w in h.lower() for w in MATERIAL_WORDS)]
    facts = [f"{e['kind'].replace('_', ' ')} on {e['date']} ({e.get('sessions_ahead', '?')} session(s) ahead)" for e in events[:3]]
    facts += [f"Material headline: {h[:110]}" for h in material[:2]]
    if contradictory:
        facts.append("Company news evidence is contradictory.")
    if governance:
        facts.append(f"{len(governance)} governance event(s) in the last 24 months.")
    return {"score": None, "direction": "negative" if (material or governance) else "neutral", "facts": facts or ["No scheduled event or material headline found in stored filings (coverage partial)."], "coverage": 1.0,
            "events_within_3_sessions": [e for e in events if e.get("sessions_ahead", 99) <= 3], "material_headlines": material, "governance_events": governance, "governance_known": governance is not None}


def market_regime(nifty: pd.Series | None, breadth_above_50dma: float | None) -> dict[str, Any]:
    """uptrend = NIFTY 50 above a rising 200-day average and more than 55% of the universe above its 50-day average; downtrend = NIFTY below a falling 200-day average or under 35% above the 50-day; else range."""
    if nifty is None or len(nifty) < 230:
        return {"score": None, "direction": "unknown", "regime": None, "facts": ["NIFTY 50 history is too short for the 200-day average."], "coverage": 0.0}
    ma = nifty.rolling(200).mean()
    above, rising = bool(nifty.iloc[-1] > ma.iloc[-1]), bool(ma.iloc[-1] > ma.iloc[-21])
    if breadth_above_50dma is None:
        return {"score": None, "direction": "unknown", "regime": None, "facts": ["Universe breadth is not available."], "coverage": 0.5}
    if above and rising and breadth_above_50dma > 0.55:
        regime = "uptrend"
    elif (not above and not rising) or breadth_above_50dma < 0.35:
        regime = "downtrend"
    else:
        regime = "range"
    score = {"uptrend": 80.0, "range": 50.0, "downtrend": 20.0}[regime]
    return {"score": score, "direction": {"uptrend": "positive", "range": "neutral", "downtrend": "negative"}[regime], "regime": regime, "coverage": 1.0,
            "facts": [f"NIFTY 50 is {'above' if above else 'below'} a {'rising' if rising else 'falling'} 200-day average; {breadth_above_50dma:.0%} of the universe is above its 50-day average: {regime}."]}
