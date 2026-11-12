"""Causal features. Every value at bar i depends on bars 0..i only (proved by ``causality.assert_causal``).

Seasonality is fitted on a TRAINING fold only and frozen for the test fold (``fit_seasonal``); fitting it on the full series is a lookahead leak.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from scalp_hold.data import bar_minutes, session_keys
from scalp_hold.venues import VenueProfile


@dataclass(frozen=True)
class FeatureCfg:
    atr_n: int = 14
    slope_minutes: int = 60
    regime_thr: float = 0.35
    htf_minutes: int = 60
    htf_ema: int = 50
    rvol_n: int = 50
    clv_n: int = 8
    vol_halflife: float = 100.0
    profile_bins: int = 40
    profile_trailing_sessions: int = 20
    value_area: float = 0.70
    node_H_minutes: int = 30
    min_bars_for_profile: int = 20


@dataclass
class Seasonal:
    """Frozen seasonal factors by time-of-day bucket (24 hourly UTC buckets for crypto, 13 half-hourly buckets for India)."""
    factors: dict[int, float] = field(default_factory=dict)
    fitted_through: str | None = None

    def factor(self, bucket: np.ndarray) -> np.ndarray:
        return np.array([self.factors.get(int(b), 1.0) for b in bucket], dtype=float) if self.factors else np.ones(len(bucket))


def tod_bucket(index: pd.DatetimeIndex, venue: VenueProfile) -> np.ndarray:
    if venue.is_india:
        local = index.tz_convert(venue.tz)
        o = venue.session_open.hour * 60 + venue.session_open.minute        # type: ignore[union-attr]
        return np.clip(((local.hour * 60 + local.minute) - o) // 30, 0, 12).astype(int)
    return np.asarray(index.hour, dtype=int)


def fit_seasonal(train: pd.DataFrame, venue: VenueProfile) -> Seasonal:
    """seasonal(bucket) = median(|logret| in bucket) / median(|logret| overall), from the training data only."""
    lr = np.abs(np.log(train["close"]).diff()).dropna()
    overall = float(lr.median())
    if not np.isfinite(overall) or overall == 0:
        return Seasonal({}, str(train.index[-1]))
    buckets = tod_bucket(lr.index, venue)
    med = lr.groupby(buckets).median()
    return Seasonal({int(b): float(v / overall) for b, v in med.items()}, str(train.index[-1]))


# ───────────────────────── anchored statistics ─────────────────────────
def _grouped_cumsum(values: pd.Series, keys: pd.Series) -> pd.Series:
    return values.groupby(keys.to_numpy()).cumsum()


def bars_since_anchor(df: pd.DataFrame, venue: VenueProfile) -> pd.Series:
    if venue.anchor_rule == "ROLLING":
        return pd.Series(np.minimum(np.arange(1, len(df) + 1), venue.profile_rolling_bars or len(df)), index=df.index)
    keys = session_keys(df.index, venue)
    return pd.Series(keys.groupby(keys.to_numpy()).cumcount().to_numpy() + 1, index=df.index)


def vwap_sigma(df: pd.DataFrame, venue: VenueProfile) -> pd.DataFrame:
    """vwap = sum(hlc3*v)/sum(v) since the anchor; sigma = sqrt(sum(hlc3^2*v)/sum(v) - vwap^2), clipped >= 0. No ATR floor."""
    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    v = df["volume"]
    # Variance is computed on prices offset by a CAUSAL reference (the first typical price of the group). Mathematically identical to the plain
    # formula, but it avoids the catastrophic cancellation of E[x^2] - E[x]^2 on large prices.
    if venue.anchor_rule == "ROLLING":
        ref = pd.Series(float(tp.iloc[0]), index=df.index)
    else:
        ref = tp.groupby(session_keys(df.index, venue).to_numpy()).transform("first")
    dev = tp - ref
    pv, p2v = dev * v, dev * dev * v
    if venue.anchor_rule == "ROLLING":
        n = venue.profile_rolling_bars or len(df)
        sv, spv, sp2v = v.rolling(n, min_periods=1).sum(), pv.rolling(n, min_periods=1).sum(), p2v.rolling(n, min_periods=1).sum()
    else:
        keys = session_keys(df.index, venue)
        sv, spv, sp2v = _grouped_cumsum(v, keys), _grouped_cumsum(pv, keys), _grouped_cumsum(p2v, keys)
    with np.errstate(divide="ignore", invalid="ignore"):
        mean_dev = spv / sv.where(sv > 0)
        vwap = ref + mean_dev
        var = (sp2v / sv.where(sv > 0) - mean_dev * mean_dev).clip(lower=0.0)
    sigma = np.sqrt(var)
    z = (df["close"] - vwap) / sigma.where(sigma > 0)
    return pd.DataFrame({"vwap": vwap, "sigma": sigma, "z": z})


def atr(df: pd.DataFrame, n: int) -> pd.Series:
    pc = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)
    tr.iloc[0] = (df["high"] - df["low"]).iloc[0]
    return tr.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()           # Wilder smoothing


def slope_regime(df: pd.DataFrame, venue: VenueProfile, vwap: pd.Series, atr_s: pd.Series, cfg: FeatureCfg) -> pd.DataFrame:
    L = max(1, cfg.slope_minutes // bar_minutes(df.index))
    bsa = bars_since_anchor(df, venue)
    with np.errstate(divide="ignore", invalid="ignore"):
        slope = (vwap - vwap.shift(L)) / atr_s.where(atr_s > 0)
    slope = slope.where(bsa > L)                         # NaN if the window crosses the anchor
    regime = pd.Series(np.where(slope.abs() >= cfg.regime_thr, np.sign(slope), 0.0), index=df.index).where(slope.notna(), 0.0)
    return pd.DataFrame({"slope_n": slope, "regime": regime})


def htf_bucket_starts(index: pd.DatetimeIndex, venue: VenueProfile, htf_minutes: int) -> pd.DatetimeIndex:
    """Start (UTC) of the HTF bucket of every chart bar; India buckets are anchored to the session open."""
    if venue.is_india:
        local = index.tz_convert(venue.tz)
        o = venue.session_open.hour * 60 + venue.session_open.minute        # type: ignore[union-attr]
        minute = local.hour * 60 + local.minute
        start_local = local.normalize() + pd.to_timedelta(o + ((minute - o) // htf_minutes) * htf_minutes, unit="min")
        return start_local.tz_convert("UTC")
    return index.floor(f"{htf_minutes}min")


def htf_bias(df: pd.DataFrame, venue: VenueProfile, cfg: FeatureCfg) -> pd.Series:
    """sign(HTF close - HTF EMA), lagged by one HTF bar so a bar only ever sees COMPLETED higher-timeframe bars."""
    chart = bar_minutes(df.index)
    if cfg.htf_minutes * 60 <= chart * 60:
        raise ValueError(f"HTF ({cfg.htf_minutes} min) must be longer than the chart timeframe ({chart} min)")
    buckets = htf_bucket_starts(df.index, venue, cfg.htf_minutes)
    grouped = df.groupby(buckets.to_numpy())
    closes = grouped["close"].last()
    ema = closes.ewm(span=cfg.htf_ema, adjust=False, min_periods=cfg.htf_ema).mean()
    bias = np.sign(closes - ema).shift(1)                         # lag: use the previous completed HTF bar
    return pd.Series(bias.reindex(buckets.to_numpy()).to_numpy(), index=df.index).fillna(0.0)


def volume_features(df: pd.DataFrame, cfg: FeatureCfg) -> pd.DataFrame:
    v = df["volume"]
    with np.errstate(divide="ignore", invalid="ignore"):
        rvol = v / v.rolling(cfg.rvol_n, min_periods=cfg.rvol_n).mean()
        rng = (df["high"] - df["low"]).where(lambda x: x > 0)
        clv = ((2 * df["close"] - df["high"] - df["low"]) / rng).fillna(0.0)
        clv_mom = (clv * v).rolling(cfg.clv_n, min_periods=cfg.clv_n).sum() / v.rolling(cfg.clv_n, min_periods=cfg.clv_n).sum().where(lambda x: x > 0)
    return pd.DataFrame({"rvol": rvol, "clv_mom": clv_mom})


def sigma_bar(df: pd.DataFrame, cfg: FeatureCfg) -> pd.Series:
    lr = np.log(df["close"]).diff()
    return np.sqrt((lr * lr).ewm(halflife=cfg.vol_halflife, adjust=False, min_periods=20).mean())


def sigma_h(feat_sigma_bar: pd.Series, index: pd.DatetimeIndex, venue: VenueProfile, H_minutes: float, seasonal: Seasonal) -> pd.Series:
    """sigma_H = sigma_bar * sqrt(H / bar_min) * seasonal(bucket), as a fraction of price."""
    bm = bar_minutes(index)
    return feat_sigma_bar * np.sqrt(H_minutes / bm) * seasonal.factor(tod_bucket(index, venue))


# ───────────────────────── volume profile (fixed grid) ─────────────────────────
def _round_to_tick(x: float, tick: float) -> float:
    return max(tick, round(x / tick) * tick)


def session_bin_steps(df: pd.DataFrame, venue: VenueProfile, cfg: FeatureCfg) -> pd.Series:
    """bin_step per session = round_to_tick(median over the TRAILING completed sessions of (session_high - session_low) / 40). Estimated once per session."""
    keys = session_keys(df.index, venue)
    k = keys.to_numpy()
    rng = (df["high"].groupby(k).max() - df["low"].groupby(k).min())
    order = list(rng.index)
    tick = float(venue.tick_size)
    steps = {}
    for i, key in enumerate(order):
        trailing = rng.iloc[max(0, i - cfg.profile_trailing_sessions):i]
        steps[key] = _round_to_tick(float(trailing.median()) / cfg.profile_bins, tick) if len(trailing) else np.nan
    return pd.Series([steps[x] for x in k], index=df.index)


def _value_area(hist: np.ndarray, poc: int, share: float) -> tuple[int, int]:
    total, target = hist.sum(), share * hist.sum()
    lo = hi = poc
    acc = hist[poc]
    while acc < target and (lo > 0 or hi < len(hist) - 1):
        up = hist[hi + 1] if hi < len(hist) - 1 else -1.0
        dn = hist[lo - 1] if lo > 0 else -1.0
        if up >= dn:
            hi += 1
            acc += hist[hi]
        else:
            lo -= 1
            acc += hist[lo]
    del total
    return lo, hi


def volume_profile(df: pd.DataFrame, venue: VenueProfile, cfg: FeatureCfg, bsa: pd.Series, sigma_bar_s: pd.Series) -> pd.DataFrame:
    """poc / vah / val (70% value area) on a fixed grid, plus node_fwd_up / node_fwd_dn:

        node_fwd = mean(bin_vol in [breakout_level, breakout_level + dist]) / mean_bin_vol

    the thickness of the region price must travel THROUGH after a breakout (not the bin at the current close, which is tautologically thin).
    ``dist`` = sigma_bar * sqrt(node_H / bar_min) * price. Window = the session (SESSION) or the last ``profile_rolling_bars`` bars (ROLLING).
    """
    n = len(df)
    out = {c: np.full(n, np.nan) for c in ("poc", "vah", "val", "node_fwd_up", "node_fwd_dn", "bin_step")}
    steps = session_bin_steps(df, venue, cfg).to_numpy()
    tp = ((df["high"] + df["low"] + df["close"]) / 3.0).to_numpy()
    vol = df["volume"].to_numpy()
    bsa_v = bsa.to_numpy()
    sig = sigma_bar_s.to_numpy()
    close = df["close"].to_numpy()
    bm = bar_minutes(df.index)
    rolling = venue.profile_window == "ROLLING"
    window = venue.profile_rolling_bars or n
    for i in range(n):
        step = steps[i]
        if not np.isfinite(step) or bsa_v[i] < cfg.min_bars_for_profile:
            continue
        start = max(0, i - window + 1) if rolling else i - int(bsa_v[i]) + 1
        bins = np.floor(tp[start:i + 1] / step).astype(np.int64)
        if not np.isfinite(sig[i]):
            continue
        dist = sig[i] * np.sqrt(cfg.node_H_minutes / bm) * close[i]
        base = int(bins.min())
        hist = np.bincount(bins - base, weights=vol[start:i + 1]).astype(float)
        if hist.sum() <= 0:
            continue
        poc = int(hist.argmax())
        lo, hi = _value_area(hist, poc, cfg.value_area)
        vah, val = (base + hi + 1) * step, (base + lo) * step             # top edge of the highest value-area bin / bottom edge of the lowest
        mean_bin = hist.sum() / len(hist)

        def region_mean(a: float, b: float) -> float:
            ia, ib = int(np.floor(a / step)) - base, int(np.floor(b / step)) - base
            cells = np.zeros(ib - ia + 1)
            for j in range(max(ia, 0), min(ib, len(hist) - 1) + 1):
                cells[j - ia] = hist[j]
            return float(cells.mean()) / mean_bin if len(cells) else np.nan

        out["poc"][i], out["vah"][i], out["val"][i], out["bin_step"][i] = (base + poc + 0.5) * step, vah, val, step
        out["node_fwd_up"][i] = region_mean(vah, vah + dist)
        out["node_fwd_dn"][i] = region_mean(val - dist, val)
    return pd.DataFrame(out, index=df.index)


# ───────────────────────── assembly ─────────────────────────
def compute(df: pd.DataFrame, venue: VenueProfile, cfg: FeatureCfg = FeatureCfg(), *, with_profile: bool = True) -> pd.DataFrame:
    """All causal features that need no fitted state. sigma_H is applied separately with a frozen ``Seasonal`` (``sigma_h``)."""
    vs = vwap_sigma(df, venue)
    a = atr(df, cfg.atr_n)
    bsa = bars_since_anchor(df, venue)
    sr = slope_regime(df, venue, vs["vwap"], a, cfg)
    sb = sigma_bar(df, cfg)
    parts = [vs, a.rename("atr"), bsa.rename("bsa"), sr, htf_bias(df, venue, cfg).rename("htf_bias"), volume_features(df, cfg), sb.rename("sigma_bar")]
    if with_profile:
        parts.append(volume_profile(df, venue, cfg, bsa, sb))
    return pd.concat(parts, axis=1)


def feature_functions(venue: VenueProfile, cfg: FeatureCfg = FeatureCfg(), seasonal: Seasonal | None = None) -> dict:
    """name -> f(df) for the causality harness. Each takes a candle frame and returns a Series/DataFrame aligned to it."""
    seasonal = seasonal or Seasonal()
    return {
        "vwap_sigma_z": lambda d: vwap_sigma(d, venue),
        "atr": lambda d: atr(d, cfg.atr_n),
        "bars_since_anchor": lambda d: bars_since_anchor(d, venue),
        "slope_regime": lambda d: slope_regime(d, venue, vwap_sigma(d, venue)["vwap"], atr(d, cfg.atr_n), cfg),
        "htf_bias": lambda d: htf_bias(d, venue, cfg),
        "volume_features": lambda d: volume_features(d, cfg),
        "sigma_bar": lambda d: sigma_bar(d, cfg),
        "sigma_H": lambda d: sigma_h(sigma_bar(d, cfg), d.index, venue, 30.0, seasonal),
        "volume_profile": lambda d: volume_profile(d, venue, cfg, bars_since_anchor(d, venue), sigma_bar(d, cfg)),
    }
