"""Deterministic level guardrails (B04 Part A), applied to the technical engine's long setups ONLY when ``EngineConfig.guardrails`` is on (settings.level_guardrails_enabled).

These are correctness checks over completed daily candles, not strategy changes. Each rule is a plain computation with the inputs it used; none touches the entry, stop or
target *methods*; they only (a) drop or recompute targets that are impossible, (b) drop stale historical levels, and (c) downgrade ENTER_NOW to WAIT_FOR_ENTRY with a stated reason.
Thresholds are starting hypotheses, not validated rules; they live in GuardConfig. Decision support, not investment advice.

Rules:
 1 TARGET_BELOW_PRICE   every long target must lie above the entry band, the planned entry and the last close; invalid ones are dropped and R:R recomputed from the shown entry/stop.
 2 STALE_LEVEL          a swing-high-only resistance zone is not used if, after that high, price fell >= stale_drawdown_pct and has not recovered to stale_below_frac of it (crash leg).
 3 STOP_TOO_WIDE        stop further than the horizon's cap (percent of entry or daily-ATR multiples): setup is downgraded, the stop itself is unchanged.
 4 OVERHEAD_RESISTANCE  the nearest resistance above (zones, the daily 52-week high) offers less than min_rr_to_resistance R from the planned entry: WAIT, not enter.
 5 REPEATED_REJECTION   a resistance zone that rejected price >= reject_min times in the lookback with no close above it is resistance, not a breakout.
 6 CARD_INCONSISTENT    entry above stop, targets ascending and above the band and close, R:R recomputed from the shown entry/stop, prices from one completed candle.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import numpy as np
import pandas as pd

VERSION = "level-guardrails/1"
TICK = Decimal("0.05")


@dataclass
class GuardConfig:
    stop_cap_pct: dict[str, float] = field(default_factory=lambda: {"swing": 8.0, "positional": 12.0, "long_term": 20.0})
    stop_cap_daily_atr: dict[str, float] = field(default_factory=lambda: {"swing": 3.0, "positional": 6.0, "long_term": 9.0})
    min_rr_to_resistance: float = 1.5
    reject_min: int = 3
    reject_lookback: int = 252          # daily bars
    reject_gap_bars: int = 5            # two touches closer than this count as one rejection
    reject_touch_atr: float = 0.25
    stale_drawdown_pct: float = 35.0
    stale_below_frac: float = 0.9


def _fmt(x: float) -> str:
    return str((Decimal(repr(float(x))) / TICK).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * TICK)


def rejection_count(daily: pd.DataFrame, low: float, high: float, atr_d: float, cfg: GuardConfig) -> tuple[int, bool]:
    """(rejections, any_close_above) of the zone [low, high] over the lookback. A rejection = a bar that reached the zone (high >= low - touch) but closed below its low."""
    w = daily.iloc[-cfg.reject_lookback:]
    reached = (w["high"].values >= low - cfg.reject_touch_atr * atr_d) & (w["close"].values < low)
    count, last = 0, -10**9
    for i in np.flatnonzero(reached):
        if i - last >= cfg.reject_gap_bars:
            count += 1
        last = i
    return count, bool((w["close"].values > high).any())


def is_stale_high(daily: pd.DataFrame, level: float, cfg: GuardConfig) -> bool:
    """A high that sits above a later crash leg: after the last bar that reached it, price fell by stale_drawdown_pct and is still below stale_below_frac of the level."""
    hi = daily["high"].values
    touched = np.flatnonzero(hi >= level * 0.995)
    last_close = float(daily["close"].iloc[-1])
    if len(touched) == 0:
        return last_close < level * cfg.stale_below_frac and (level - float(daily["close"].min())) / level * 100 >= cfg.stale_drawdown_pct   # the level predates the stored window
    after = daily["close"].values[touched[-1]:]
    return (level - float(after.min())) / level * 100 >= cfg.stale_drawdown_pct and last_close < level * cfg.stale_below_frac


def prepare(zones: list[dict[str, Any]], daily: pd.DataFrame, atr_d: float | None, cfg: GuardConfig | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Rule 2 and 5 inputs: drop stale swing-high-only resistance zones, annotate every remaining resistance zone with its rejection count."""
    cfg = cfg or GuardConfig()
    info: dict[str, Any] = {"dropped_levels": [], "rejection_zones": []}
    if not atr_d or len(daily) < 30:
        return zones, info
    kept = []
    for z in zones:
        if z["kind"] == "resistance":
            swing_only = set(z["families"]) <= {"swing"}
            if swing_only and is_stale_high(daily, float(z["mid"]), cfg):
                info["dropped_levels"].append({"code": "STALE_LEVEL", "low": _fmt(z["low"]), "high": _fmt(z["high"]), "reason": "historical high above a later crash leg; price has not recovered to it"})
                continue
            n, above = rejection_count(daily, float(z["low"]), float(z["high"]), atr_d, cfg)
            z = {**z, "rejections": n, "closed_above": above}
            if n >= cfg.reject_min and not above:
                info["rejection_zones"].append({"low": _fmt(z["low"]), "high": _fmt(z["high"]), "rejections": n})
        kept.append(z)
    return kept, info


def apply(horizon: str, daily: pd.DataFrame, ind: dict[str, Any], atr_d: float | None, band: list[float], setup: str, plan: dict[str, Any], status: str,
          zones: list[dict[str, Any]], prep: dict[str, Any], min_rr_first: float, cfg: GuardConfig | None = None) -> tuple[dict[str, Any], str, dict[str, Any]]:
    """-> (plan with valid targets only, status, guard report). Never changes the stop or entry; only targets, status and flags."""
    cfg = cfg or GuardConfig()
    close, entry, stop, risk = float(ind["close"]), float(plan["entry"]), float(plan["stop"]), float(plan["risk"])
    flags: list[dict[str, str]] = []
    report: dict[str, Any] = {"version": VERSION, "flags": flags, "dropped_levels": list(prep.get("dropped_levels", [])), "rejection_zones": list(prep.get("rejection_zones", []))}
    floor = max(float(band[1]), entry, close)
    # 1. targets must be above the band, the planned entry and the last close
    kept, bad = [], []
    for t in plan["targets"]:
        (kept if float(t["price"]) > floor else bad).append(t)
    for t in bad:
        flags.append({"code": "TARGET_BELOW_PRICE", "detail": f"{t['label']} {t['price']} is not above the entry band / last close ({_fmt(floor)}); dropped."})
    targets = [dict(t) for t in kept]
    for m in (1.5, 2.5, 4.0):                         # refill only with labelled risk-reward targets that are themselves valid
        if bad and len(targets) < 3:
            p = entry + m * risk
            if p > floor and (not targets or p > float(targets[-1]["price"]) + 0.25 * risk):
                targets.append({"basis": f"Risk-reward based ({m:g}R); market structure offers no nearer level", "price": _fmt(p)})
    for i, t in enumerate(targets, 1):
        t["label"], t["risk_reward"] = f"T{i}", round((float(t["price"]) - entry) / risk, 2)
    plan = {**plan, "targets": targets, "rr_first": targets[0]["risk_reward"] if targets else 0}
    plan["enough_upside"] = bool(targets) and plan["rr_first"] >= min_rr_first
    if not targets:
        flags.append({"code": "NO_VALID_TARGET", "detail": "No valid target above the entry band and last close remains; no target is shown."})
        plan["enough_upside"] = False
    # 3. stop width
    pct = risk / entry * 100
    d_atr = risk / atr_d if atr_d else None
    if pct > cfg.stop_cap_pct[horizon] or (d_atr is not None and d_atr > cfg.stop_cap_daily_atr[horizon]):
        flags.append({"code": "STOP_TOO_WIDE", "detail": f"Stop is {pct:.1f}% / {d_atr:.1f} daily ATR from entry (cap {cfg.stop_cap_pct[horizon]:g}% or {cfg.stop_cap_daily_atr[horizon]:g} ATR for {horizon}); risk is large for the upside." if d_atr is not None else f"Stop is {pct:.1f}% from entry (cap {cfg.stop_cap_pct[horizon]:g}%)."})
    # 4. overhead resistance (zones above the entry, plus the daily 52-week high)
    overhead = [(max(float(z["low"]), close), f"resistance zone {_fmt(z['low'])}-{_fmt(z['high'])}") for z in zones if z["kind"] == "resistance" and float(z["high"]) > entry]
    hi52 = float(daily["high"].iloc[-252:].max())
    if hi52 > entry:
        overhead.append((hi52, f"52-week high {_fmt(hi52)}"))
    if overhead:
        level, name = min(overhead, key=lambda t: t[0])
        rr = (level - entry) / risk
        if rr < cfg.min_rr_to_resistance:
            flags.append({"code": "OVERHEAD_RESISTANCE", "detail": f"Next resistance ({name}) is only {max(rr, 0):.1f}R above the planned entry (minimum {cfg.min_rr_to_resistance:g}R); wait."})
    # 5. repeated rejection: a zone that rejected price repeatedly and was never closed above is resistance, not a breakout
    for z in zones:
        if z["kind"] == "resistance" and z.get("rejections", 0) >= cfg.reject_min and not z.get("closed_above") and float(z["high"]) > entry and float(z["low"]) - entry < 3 * risk:
            flags.append({"code": "REPEATED_REJECTION", "detail": f"Zone {_fmt(z['low'])}-{_fmt(z['high'])} rejected price {z['rejections']} times in the last {cfg.reject_lookback} sessions with no close above; treated as resistance, not a breakout."})
            break
    if flags and status == "ENTER_NOW":
        status = "WAIT_FOR_ENTRY"
    report["downgraded"] = bool(flags)
    return plan, status, report


def validate_card(card: dict[str, Any], last_close: float) -> list[str]:
    """Rule 6: internal consistency of a rendered horizon card. Returns violations (empty = consistent)."""
    out: list[str] = []
    entry, stop, targets = card.get("entry"), card.get("stop_loss"), card.get("targets") or []
    if not entry or not stop:
        return out
    lo, hi, planned, sp = float(entry["low"]), float(entry["high"]), float(entry["planned_price"]), float(stop["price"])
    if not sp < lo:
        out.append("stop is not below the entry band")
    if not (lo - 1e-9 <= planned <= hi + 1e-9) and card.get("entry_status") == "ENTER_NOW":
        out.append("planned entry is outside the entry band")
    prices = [float(t["price"]) for t in targets]
    if prices != sorted(set(prices)):
        out.append("targets are not strictly ascending")
    for t in targets:
        p = float(t["price"])
        if p <= max(hi, last_close):
            out.append(f"{t['label']} {t['price']} is not above the entry band and last close")
        rr = (p - planned) / (planned - sp) if planned > sp else None
        tol = 0.03 + 0.1 * (1 + float(t["risk_reward"])) / max(planned - sp, 1e-9)       # prices are shown to the 0.05 tick, so a recomputed R:R may differ by tick rounding
        if rr is None or abs(rr - float(t["risk_reward"])) > tol:
            out.append(f"{t['label']} R:R {t['risk_reward']} does not match entry/stop/target (recomputed {rr and round(rr, 2)})")
    if abs(float(card["current_price"]) - last_close) > 0.026:
        out.append("current price is not the last completed daily close")
    return out


def finalize(card: dict[str, Any], last_close: float, guard: dict[str, Any] | None) -> dict[str, Any]:
    guard = guard or {"version": VERSION, "flags": [], "dropped_levels": [], "rejection_zones": [], "downgraded": False}
    problems = validate_card(card, last_close)
    if problems:
        guard["flags"].append({"code": "CARD_INCONSISTENT", "detail": "; ".join(problems)})
        guard["downgraded"] = True
        if card.get("entry_status") == "ENTER_NOW":
            card["entry_status"] = "WAIT_FOR_ENTRY"
    card["guardrails"] = guard
    return card
