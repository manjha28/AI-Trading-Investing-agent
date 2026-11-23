"""Measured win-probability confidence (Phase 7b). Replaces the evidence score as the number shown as "confidence"; the evidence score E is still computed and stored for research.

Meaning: the estimated chance (0-100) that this setup wins, learned from similar past setups and pulled toward the horizon's average when evidence is thin.
  win (swing)       T1 reached before the stop within 15 sessions, after delivery costs (technical_backtest.simulate_card)
  win (positional)  the same within 60 sessions
  win (long_term)   beats the NIFTY 500 price index over the next 12 months (point-in-time test, quarterly NSE history)
Method (all in ConfidenceConfig): base rate p0 = share of all past setups of the horizon that won; similar setups = past setups of the same horizon that match on a fixed set of point-in-time features,
dropping the weakest feature one at a time until at least ``min_matches`` remain; beta-binomial shrinkage confidence = (w + k*p0) / (n + k); if a required lens is unknown n (and w) are halved first. Bands are
relative to p0 (High: >= 10 points above p0 and n >= 100; Low: at or below p0; Medium otherwise). Every band is labelled "not yet separating" until the stored validation shows High beating Low on net R.
Pure functions over a stored model file (``win_confidence_model.json``, built by ``build_model`` from the replay dataset); nothing here fetches data, and a model never emits a price or a level.
Decision support, not investment advice."""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

import pandas as pd

MODEL_PATH = Path(__file__).with_name("win_confidence_model.json")
TRADE_FEATURES = ("setup", "stack", "rs60", "dist52", "stop_pct", "regime", "size")          # swing and positional
LONG_FEATURES = ("klass", "q", "g")                                                           # long term: class plus Q and G terciles
NUMERIC = {"rs60": 3, "dist52": 3, "stop_pct": 3, "q": 3, "g": 3}                             # tercile-binned features
TERCILE_LABELS = ("low", "mid", "high")


@dataclass(frozen=True)
class ConfidenceConfig:
    k: float = 50.0                       # shrinkage strength (pseudo-setups at the base rate)
    min_matches: int = 30                 # drop the weakest feature until at least this many past setups match
    data_penalty: float = 0.5             # halve n (and wins) when a required lens is unknown
    high_margin: float = 0.10             # High = at least this far above the base rate ...
    high_min_n: int = 100                 # ... with at least this many (effective) matches
    interval: float = 0.80                # credible interval shown in the tooltip
    size_cuts_cr: tuple[float, float] = (10.0, 50.0)   # 20-session median traded value, Rs crore: small < 10 <= mid < 50 <= large


# ───────────────────────── features ─────────────────────────
def series_features(close: pd.Series, high: pd.Series, bench_close: pd.Series | None, *, bars_52w: int = 252) -> dict[str, float | int | None]:
    """The chart features from completed daily bars only, date-indexed (the last bar is the decision close): trend stack (0-4 of close>EMA20, EMA20>EMA50, EMA50>EMA200, close>EMA200), 60-bar return minus the
    benchmark's over the same dates (rs60), and the distance of the close from the 52-week high (<= 0). Unknown (None) when the history is too short; never zero."""
    c = close.astype("float64")
    out: dict[str, float | int | None] = {"stack": None, "rs60": None, "dist52": None}
    if len(c) >= 200:
        e20, e50, e200 = (c.ewm(span=sp, adjust=False).mean().iloc[-1] for sp in (20, 50, 200))
        last = float(c.iloc[-1])
        out["stack"] = int(last > e20) + int(e20 > e50) + int(e50 > e200) + int(last > e200)
        h52 = float(high.astype("float64").iloc[-bars_52w:].max())
        out["dist52"] = last / h52 - 1 if h52 > 0 else None
    if bench_close is not None and len(c) > 60 and len(bench_close) > 60:
        d0, d1 = c.index[-61], c.index[-1]
        b0, b1 = bench_close.loc[:d0], bench_close.loc[:d1]
        if len(b0) and len(b1) and float(b0.iloc[-1]) > 0 and (d0 - b0.index[-1]).days <= 7 and (d1 - b1.index[-1]).days <= 7:
            out["rs60"] = float(c.iloc[-1] / c.iloc[-61] - 1) - float(b1.iloc[-1] / b0.iloc[-1] - 1)
    return out


def setup_type(card: dict[str, Any]) -> str:
    """blue_sky when T1 has no resistance above (its basis says so), else the engine's selected setup: breakout, pullback (which includes a retest of a broken level) or momentum."""
    targets = card.get("targets") or []
    basis = str((targets[0] or {}).get("basis") or "") if targets else ""
    if "Blue sky" in basis:
        return "blue_sky"
    return {"BREAKOUT_ENTRY": "breakout", "PULLBACK_ENTRY": "pullback", "MOMENTUM_ENTRY": "momentum"}.get(str(card.get("selected_setup") or ""), "other")


def size_bucket(median_traded_value_cr: float | None, cfg: ConfidenceConfig | None = None) -> str | None:
    cfg = cfg or ConfidenceConfig()
    if median_traded_value_cr is None:
        return None
    lo, hi = cfg.size_cuts_cr
    return "small" if median_traded_value_cr < lo else "mid" if median_traded_value_cr < hi else "large"


def tercile(value: float | None, cuts: Sequence[float] | None) -> str | None:
    if value is None or cuts is None or value != value:
        return None
    return TERCILE_LABELS[0] if value <= cuts[0] else TERCILE_LABELS[1] if value <= cuts[1] else TERCILE_LABELS[2]


def bin_trade_features(raw: dict[str, Any], cuts: dict[str, Sequence[float]]) -> dict[str, str | None]:
    """raw: setup, stack, rs60, dist52, stop_pct, regime, size (None = unknown). Returns the binned features a model matches on."""
    return {"setup": raw.get("setup"), "stack": None if raw.get("stack") is None else ("full" if raw["stack"] == 4 else "mixed" if raw["stack"] >= 2 else "weak"),
            "rs60": tercile(raw.get("rs60"), cuts.get("rs60")), "dist52": tercile(raw.get("dist52"), cuts.get("dist52")), "stop_pct": tercile(raw.get("stop_pct"), cuts.get("stop_pct")),
            "regime": raw.get("regime"), "size": raw.get("size")}


def bin_long_features(raw: dict[str, Any], cuts: dict[str, Sequence[float]]) -> dict[str, str | None]:
    return {"klass": raw.get("klass"), "q": tercile(raw.get("q"), cuts.get("q")), "g": tercile(raw.get("g"), cuts.get("g"))}


def tercile_cuts(values: Iterable[float | None]) -> list[float] | None:
    s = pd.Series([v for v in values if v is not None and v == v], dtype="float64")
    if len(s) < 30:
        return None
    return [float(s.quantile(1 / 3)), float(s.quantile(2 / 3))]


# ───────────────────────── statistics ─────────────────────────
def shrink(wins: float, n: float, p0: float, k: float) -> float:
    """Beta-binomial posterior mean with a Beta(k*p0, k*(1-p0)) prior: (w + k*p0) / (n + k)."""
    return (wins + k * p0) / (n + k)


def _betacf(a: float, b: float, x: float) -> float:
    tiny, qab, qap, qam = 1e-30, a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 300):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d; d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c; c = c if abs(c) > tiny else tiny
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d; d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c; c = c if abs(c) > tiny else tiny
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-12:
            break
    return h


def beta_cdf(x: float, a: float, b: float) -> float:
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    lbeta = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
    front = math.exp(lbeta + a * math.log(x) + b * math.log(1.0 - x))
    return front * _betacf(a, b, x) / a if x < (a + 1.0) / (a + b + 2.0) else 1.0 - front * _betacf(b, a, 1.0 - x) / b


def beta_ppf(q: float, a: float, b: float) -> float:
    lo, hi = 0.0, 1.0
    for _ in range(60):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if beta_cdf(mid, a, b) < q else (lo, mid)
    return (lo + hi) / 2


def interval(wins: float, n: float, p0: float, cfg: ConfidenceConfig) -> tuple[float, float]:
    a, b = wins + cfg.k * p0, (n - wins) + cfg.k * (1.0 - p0)
    tail = (1.0 - cfg.interval) / 2
    return beta_ppf(tail, a, b), beta_ppf(1.0 - tail, a, b)


def feature_order(rows: Sequence[dict[str, Any]], features: Sequence[str]) -> list[str]:
    """Weakest feature first: the spread of the win rate across a feature's bins (count-weighted variance of the bin win rates around the overall rate). Computed from the setups themselves, so it is refit with them."""
    if not rows:
        return list(features)
    p = sum(1 for r in rows if r["win"]) / len(rows)
    score = {}
    for f in features:
        groups: dict[Any, list[int]] = {}
        for r in rows:
            v = r["bins"].get(f)
            if v is not None:
                groups.setdefault(v, []).append(1 if r["win"] else 0)
        tot = sum(len(g) for g in groups.values())
        score[f] = 0.0 if not tot else sum(len(g) * (sum(g) / len(g) - p) ** 2 for g in groups.values()) / tot
    return sorted(features, key=lambda f: (score[f], f))


def find_matches(rows: Sequence[dict[str, Any]], bins: dict[str, str | None], order: Sequence[str], min_matches: int) -> tuple[list[dict[str, Any]], list[str]]:
    """Past setups matching on every kept feature; the weakest feature is dropped one at a time until at least min_matches remain. An unknown feature on the new setup cannot match, so it is dropped up front."""
    used = [f for f in order if bins.get(f) is not None]
    while True:
        m = [r for r in rows if all(r["bins"].get(f) == bins[f] for f in used)]
        if len(m) >= min_matches or not used:
            return m, used
        drop = next((f for f in order if f in used), None)
        used = [f for f in used if f != drop]


# ───────────────────────── scoring ─────────────────────────
def score(horizon: str, bins: dict[str, str | None], model: dict[str, Any], *, required_unknown: bool = False, cfg: ConfidenceConfig | None = None) -> dict[str, Any]:
    cfg = cfg or ConfidenceConfig()
    hz = (model.get("horizons") or {}).get(horizon)
    if not hz or not hz.get("setups"):
        return {"available": False, "reason": "No measured history for this horizon yet.", "band": None, "line": None, "calibrated": None}
    p0 = float(hz["p0"])
    rows = hz["setups"]
    matches, used = find_matches(rows, bins, hz["order"], cfg.min_matches)
    n_raw, w_raw = len(matches), sum(1 for r in matches if r["win"])
    f = cfg.data_penalty if required_unknown else 1.0
    n, w = n_raw * f, w_raw * f
    conf = shrink(w, n, p0, cfg.k)
    lo, hi = interval(w, n, p0, cfg)
    band = "Low" if conf <= p0 else "High" if (conf - p0 >= cfg.high_margin and n >= cfg.high_min_n) else "Medium"
    nets = [r["net_r"] for r in matches if r.get("net_r") is not None]
    ev = None if not nets else sum(nets) / len(nets)
    separating = bool((hz.get("validation") or {}).get("separating"))
    pct, base = round(conf * 100), round(p0 * 100)
    win_text = {"swing": "reached T1 before the stop within 15 sessions", "positional": "reached T1 before the stop within 60 sessions", "long_term": "beat the NIFTY 500 over the next 12 months"}[horizon]
    return {"available": True, "calibrated": pct, "win_probability": round(conf, 4), "base_rate": round(p0, 4), "base_rate_pct": base, "n_matches": n_raw, "n_effective": round(n, 1), "wins": w_raw,
            "interval": [round(lo, 4), round(hi, 4)], "features_used": used, "features_dropped": [f for f in hz["order"] if f not in used], "band": band, "separating": separating,
            "band_label": band if separating else f"{band} (not yet separating)", "expected_r": None if ev is None else round(ev, 3), "data_penalty_applied": bool(required_unknown), "win_definition": win_text,
            "line": f"Confidence {pct}% (avg setup {base}%) · based on {n_raw} similar past setups", "tooltip": f"{round(cfg.interval * 100)}% interval {round(lo * 100)}-{round(hi * 100)}%. Win = {win_text}."
            + (f" Matched on: {', '.join(used) or 'nothing (all setups)'}." ) + (" A required input is unknown, so the evidence was halved." if required_unknown else "")
            + ("" if ev is None else f" Matched setups' average net R {ev:+.2f}."), "model_version": model.get("version"), "validated": separating}


_MODEL: dict[str, Any] | None = None


def load_model(path: Path | None = None) -> dict[str, Any]:
    global _MODEL
    if path is not None:
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return {}
    if _MODEL is None:
        try:
            _MODEL = json.loads(MODEL_PATH.read_text())
        except (OSError, ValueError):
            _MODEL = {}
    return _MODEL


def confidence_for_card(horizon: str, *, card: dict[str, Any] | None, close: pd.Series | None, high: pd.Series | None, bench_close: pd.Series | None, regime: str | None,
                        median_traded_value_cr: float | None, required_unknown: bool, mb: dict[str, Any] | None = None, model: dict[str, Any] | None = None) -> dict[str, Any]:
    """The live confidence for one stock and horizon from the same features the model was built on. Swing/positional need a valid card; long term needs the multibagger class."""
    model = model if model is not None else load_model()
    hz = (model.get("horizons") or {}).get(horizon) or {}
    cuts = hz.get("cuts") or {}
    if horizon == "long_term":
        if not mb or not mb.get("class"):
            return {"available": False, "reason": "No long-term class yet.", "band": None, "line": None, "calibrated": None}
        sc = mb.get("scores") or {}
        bins = bin_long_features({"klass": _lt_class(mb["class"]), "q": sc.get("Q"), "g": sc.get("G")}, cuts)
        return score(horizon, bins, model, required_unknown=required_unknown)
    if not card or card.get("status") != "available" or close is None or high is None:
        return {"available": False, "reason": "No valid setup to measure.", "band": None, "line": None, "calibrated": None}
    sf = series_features(close, high, bench_close)
    stop_pct = card.get("stop_percent")
    try:
        stop_pct = None if stop_pct is None else float(stop_pct)
    except (TypeError, ValueError):
        stop_pct = None
    raw = {"setup": setup_type(card), "stack": sf["stack"], "rs60": sf["rs60"], "dist52": sf["dist52"], "stop_pct": stop_pct, "regime": regime, "size": size_bucket(median_traded_value_cr)}
    return score(horizon, bin_trade_features(raw, cuts), model, required_unknown=required_unknown or any(v is None for v in (sf["rs60"], sf["stack"])))


def _lt_class(klass: str) -> str:
    return "compounder" if klass in ("quality_compounder", "multibagger_candidate") else klass


# ───────────────────────── model building / walk-forward (used by the research scripts) ─────────────────────────
def fit_horizon(setups: Sequence[dict[str, Any]], features: Sequence[str]) -> dict[str, Any]:
    """setups: dicts with bins, win, net_r. Returns the stored per-horizon block (p0, order, setups)."""
    rows = [{"bins": s["bins"], "win": bool(s["win"]), "net_r": s.get("net_r"), "finish": s.get("finish")} for s in setups]
    p0 = sum(1 for r in rows if r["win"]) / len(rows)
    nets = [r["net_r"] for r in rows if r["net_r"] is not None]
    return {"p0": p0, "n": len(rows), "avg_net_r": None if not nets else sum(nets) / len(nets), "order": feature_order(rows, features), "setups": rows}
