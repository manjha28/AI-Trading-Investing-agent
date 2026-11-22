"""Nexus v2 level rules (Phase 1, flag ``levels_v2_enabled``; off by default, flags off = v1 byte-identical).

Pure, deterministic functions over completed candles. The technical engine calls them only when ``EngineConfig.levels_v2`` is on. Rules:
  1  bearish veto: a bearish read or DOWNTREND regime is never BUY and never gets a ladder (a watch band may be shown on the Radar only)
  2  every target at least last close + target_margin_atr x daily ATR (invalid targets are dropped and refilled from structure)
  3  one R definition: R = planned entry minus stop
  4  T1 is the NEAREST barrier resistance (swing high / 52-week high / multi-factor zone) above entry; none may sit between entry and T1; T1 >= min R:R or no setup
  5  headroom: no ENTER_NOW when the next barrier is under headroom_r R above the close
  6  repeated rejections: >= reject_min failed tests and no close above = range-bound: no MOMENTUM_ENTRY
  7  stop = structural level + ATR buffer, within [min daily ATR (swing), max percent]; outside the bounds there is no setup (the stop is never silently moved)
  8  recency: swing points limited per frame, true 52-week high from 252 daily bars, no R-multiple target above max_target_r
  9  one status rule: band_position from ONE shared close; a band above the market is a breakout trigger; a pullback band under a broken support is invalid
 10  split / bonus: detect raw vs already-adjusted bars at each ex-date and adjust only raw bars
 14  ``ready_to_buy``: in band or ENTER_NOW and not headroom-blocked
Every threshold is in LevelsV2Config (starting hypotheses, not validated rules). Decision support, not investment advice.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import numpy as np
import pandas as pd

TICK = Decimal("0.05")
R_DEFINITION = "R = planned entry minus stop. A target's R:R = (target - planned entry) / R. For cards in the band the R:R from the last close is also shown."
BARRIER_FAMILIES = {"swing", "period"}


@dataclass
class LevelsV2Config:
    min_rr_t1: dict[str, float] = field(default_factory=lambda: {"swing": 1.5, "positional": 1.5, "long_term": 1.5})
    target_margin_atr: float = 0.25                  # every target >= last close + this x daily ATR
    headroom_r: float = 1.0                          # no ENTER_NOW with a barrier closer than this many R above the close
    max_stop_pct: dict[str, float] = field(default_factory=lambda: {"swing": 8.0, "positional": 12.0, "long_term": 20.0})
    min_stop_daily_atr: dict[str, float] = field(default_factory=lambda: {"swing": 1.0, "positional": 0.0, "long_term": 0.0})
    min_stop_primary_atr: float = 0.4                # existing engine floor kept
    max_target_r: float = 5.0                        # no risk-reward-multiple target above this
    refill_multiples: tuple[float, ...] = (1.5, 2.5, 4.0)
    target_buffer_atr: float = 0.1                   # target sits this far below the resistance zone's low (as in v1)
    recency_bars: dict[str, int] = field(default_factory=lambda: {"D": 500, "W": 104, "M": 36})
    broken_support_lookback: dict[str, int] = field(default_factory=lambda: {"D": 120, "W": 52, "M": 24})
    high52_bars: int = 252
    major_swing_k: dict[str, int] = field(default_factory=lambda: {"D": 10, "W": 5, "M": 3})      # a MAJOR swing high is confirmed by this many candles each side
    major_fall_atr: float = 2.0                      # ... and was followed by a fall of at least this many ATR ...
    major_fall_pct: float = 8.0                      # ... or this many percent (either one) ...
    major_min_rejections: int = 2                    # ... or price was rejected at it at least this many times
    major_touch_atr: float = 0.25                    # a rejection = a candle reaching within this many ATR of the level and closing below it
    major_reject_gap_bars: int = 5                   # touches closer than this count once
    broken_close_atr: float = 0.5                    # once any close is above the level by this many ATR it is broken, no longer a wall
    use_major_swing_barriers: bool = True            # relaxation switch (default = as specified): when False, only multi-factor zones and the 52-week high are barriers
    high52_blue_sky_atr: float = 0.5                 # PROVISIONAL v2 default (owner decision 2026-11-22; v1 untouched): a 52-week high less than this many daily ATR above the close is NOT a barrier (the stock is at its high).
                                                     # Evidence: +303 ladders, added setups net R +0.28 [+0.10, +0.46]; with 8 comparisons it would not survive a multiple-testing correction, so every ladder that exists only
                                                     # because of it is flagged (``relaxation_dependent``) and tracked separately in shadow mode. 0 = off (the original spec).
    broken_support_veto: bool = True                 # relaxation switch (default on, as specified)
    blue_sky_weekly_atr: float = 2.0                 # no barrier above the close: T1 = max(2R, close + this x weekly ATR)
    blue_sky_min_r: float = 2.0


def _fmt(x: float) -> str:
    return str((Decimal(repr(float(x))) / TICK).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * TICK)


def bearish_veto(direction: str | None, regime: str | None) -> str | None:
    if direction in ("BEARISH", "STRONG_BEARISH") or regime == "DOWNTREND":
        return (f"The engine reads {str(direction or 'unknown').lower().replace('_', ' ')} in a {str(regime or 'unknown').lower().replace('_', ' ')} regime: timing does not support a buy, "
                "so no entry ladder is shown. A watch band may appear on the Radar only.")
    return None


def is_barrier(z: dict[str, Any]) -> bool:
    """A barrier is a multi-factor zone (strength >= 2: e.g. a double top, or a swing high at a moving average / pivot) or the 52-week high. A lone minor fractal swing high is
    NOT a barrier on its own (nearly every stock has one a few percent overhead, which would block almost every setup); MAJOR swing highs enter through ``extra``."""
    return float(z["strength"]) >= 2 or "52-week high" in z["labels"]


def qualify_swing_highs(highs: list[tuple[int, float]], df: pd.DataFrame, atr_p: float, cfg: LevelsV2Config) -> list[tuple[float, str]]:
    """Major swing highs that count as barriers (``highs`` are pivots already confirmed by the horizon's pivot width, inside the lookback window ``df``).
    A pivot is a barrier only if (a) it was followed by a fall of >= major_fall_atr ATR or >= major_fall_pct percent, or price was rejected there >= major_min_rejections times,
    and (b) no close since has been above it by >= broken_close_atr ATR (once broken it is no longer a wall). Minor swing highs are never barriers."""
    hi, lo, cl = df["high"].to_numpy(), df["low"].to_numpy(), df["close"].to_numpy()
    out: list[tuple[float, str]] = []
    for i, v in highs:
        if i + 1 >= len(df):
            continue
        if (cl[i + 1:] >= v + cfg.broken_close_atr * atr_p).any():
            continue
        fall = v - float(lo[i + 1:].min())
        reached = (hi[i + 1:] >= v - cfg.major_touch_atr * atr_p) & (cl[i + 1:] < v)
        n, last = 0, -10**9
        for j in np.flatnonzero(reached):
            if j - last >= cfg.major_reject_gap_bars:
                n += 1
            last = j
        if fall >= cfg.major_fall_atr * atr_p or fall / v * 100 >= cfg.major_fall_pct or n >= cfg.major_min_rejections:
            out.append((float(v), "major swing high"))
    return out


def barriers_above(zones: list[dict[str, Any]], price: float, extra: list[tuple[float, str]] | None = None) -> list[dict[str, Any]]:
    """Barrier resistances above ``price`` (use max(entry, close): a level price already trades above is broken, not a barrier). ``extra`` adds explicit single levels
    (the true 52-week high, recent swing highs), because a cluster whose AVERAGE is below the close is typed support even when its top edge is overhead."""
    out = [z for z in zones if z["kind"] == "resistance" and float(z["high"]) > price and is_barrier(z)]
    out += [{"low": v, "high": v, "labels": [label], "families": ["swing"], "strength": 1.0, "kind": "resistance", "rejections": 0, "closed_above": False} for v, label in (extra or []) if v > price]
    return sorted(out, key=lambda z: float(z["low"]))


def range_bound_zone(zones: list[dict[str, Any]], close: float, reject_min: int = 3) -> dict[str, Any] | None:
    """The nearest barrier above the close, if price has failed to close above it reject_min or more times (zones are annotated by level_guardrails.prepare)."""
    near = barriers_above(zones, close)
    z = near[0] if near else None
    return z if z is not None and z.get("rejections", 0) >= reject_min and not z.get("closed_above") else None


def broken_support_above(band_hi: float, close: float, recent_lows: list[float]) -> float | None:
    """A recent swing low above the band that price has already closed below: the pullback band would sit under a broken support."""
    higher = [v for v in recent_lows if v > band_hi and close < v]
    return max(higher) if higher else None


def high52(daily: pd.DataFrame, bars: int = 252) -> float:
    """True 52-week high: the highest high of the last ``bars`` completed DAILY candles, window edges explicit (the candle at position -bars is IN, -bars-1 is OUT)."""
    return float(daily["high"].iloc[-bars:].max())


def band_position(close: float, lo: float, hi: float) -> str:
    return "inside" if lo <= close <= hi else "above" if close > hi else "below"


def plan(horizon: str, setup: str, status: str, scen: dict[str, Any], ind: dict[str, Any], atr_d: float | None, zones: list[dict[str, Any]], swing_lows: list[float],
         buffer_atr: float, cfg: LevelsV2Config, extra: list[tuple[float, str]] | None = None, weekly_atr: float | None = None) -> tuple[dict[str, Any] | None, str | None]:
    """-> (plan, None) or (None, reason). Entry method is v1's; stop and targets follow the v2 rules."""
    a, close = float(ind["atr14"]), float(ind["close"])
    ad = float(atr_d or a)
    zone = scen["zone"]
    entry = close if status == "ENTER_NOW" else scen["support_zone"].get("core", scen["support_zone"]["mid"]) if setup == "PULLBACK_ENTRY" else scen["reference"] if setup == "BREAKOUT_ENTRY" else zone[1]
    entry = min(max(entry, zone[0]), max(zone[1], close)) if status == "ENTER_NOW" else entry
    if setup == "PULLBACK_ENTRY":
        structure = scen["support_zone"]["low"]
    elif setup == "BREAKOUT_ENTRY":
        structure = scen["resistance_zone"]["low"]
    else:
        below = [z for z in zones if z["kind"] == "support" and z["high"] < entry]
        structure = max((z["low"] for z in below), default=min(swing_lows[-1:] or [entry - 2 * a]))
    stop = structure - buffer_atr * a
    risk = entry - stop
    pct = risk / entry * 100 if entry else 0.0
    if risk <= 0 or risk < cfg.min_stop_primary_atr * a:
        return None, f"The structural stop ({_fmt(stop)}) is not a usable distance from entry ({_fmt(entry)})."
    if risk < cfg.min_stop_daily_atr[horizon] * ad:
        return None, f"The structural stop is {risk / ad:.1f} daily ATR from entry, inside one day's range (minimum {cfg.min_stop_daily_atr[horizon]:g}); no valid stop."
    if pct > cfg.max_stop_pct[horizon]:
        return None, f"The structural stop is {pct:.1f}% from entry, wider than the {cfg.max_stop_pct[horizon]:g}% limit for {horizon.replace('_', '-')}; no valid stop (it is not moved)."
    if zone[1] <= stop:
        return None, "The whole entry band sits below its own stop."
    floor = max(entry, close + cfg.target_margin_atr * ad, zone[1])
    barriers = barriers_above(zones, max(entry, close), extra)
    targets: list[dict[str, Any]] = []
    t1_zone = barriers[0] if barriers else None
    if t1_zone is not None:
        p1 = float(t1_zone["low"]) - cfg.target_buffer_atr * a
        if p1 <= floor:
            return None, (f"The nearest resistance ({_fmt(t1_zone['low'])}-{_fmt(t1_zone['high'])}, {', '.join(t1_zone['labels'][:2])}) leaves no room above the last close; "
                          f"first target would be {_fmt(p1)} against a floor of {_fmt(floor)}.")
        targets.append({"_p": p1, "basis": f"Resistance zone {_fmt(t1_zone['low'])}-{_fmt(t1_zone['high'])} ({', '.join(t1_zone['labels'][:2])}): nearest barrier above entry"})
    else:                                             # blue sky (no barrier above the close): T1 = the larger of 2R and close + 2 weekly ATR
        p_r = entry + cfg.blue_sky_min_r * risk
        p_w = close + cfg.blue_sky_weekly_atr * weekly_atr if weekly_atr else 0.0
        p = max(p_r, p_w)
        targets.append({"_p": p, "basis": f"Blue sky, no resistance: the larger of {cfg.blue_sky_min_r:g}R and the close plus {cfg.blue_sky_weekly_atr:g} weekly ATR" + ("" if weekly_atr else " (weekly ATR unavailable, 2R used)"), "blue_sky": True})
    if round((targets[0]["_p"] - entry) / risk, 2) < cfg.min_rr_t1[horizon]:
        z = t1_zone
        return None, (f"First target {_fmt(targets[0]['_p'])} is only {(targets[0]['_p'] - entry) / risk:.2f}R from the planned entry "
                      f"(minimum {cfg.min_rr_t1[horizon]:g}R)" + (f"; nearest resistance {_fmt(z['low'])} ({', '.join(z['labels'][:2])}) blocks the way." if z else "."))
    for z in sorted((z for z in zones if z["kind"] == "resistance" and float(z["low"]) > targets[0]["_p"]), key=lambda z: float(z["low"])):
        p = float(z["low"]) - cfg.target_buffer_atr * a
        if p > floor and p - targets[-1]["_p"] >= 0.5 * risk and len(targets) < 3:
            targets.append({"_p": p, "basis": f"Resistance zone {_fmt(z['low'])}-{_fmt(z['high'])} ({', '.join(z['labels'][:2])})"})
    for m in cfg.refill_multiples:
        if len(targets) >= 3:
            break
        p = entry + m * risk
        if m <= cfg.max_target_r and p > floor and p > targets[-1]["_p"] + 0.25 * risk:
            targets.append({"_p": p, "basis": f"Risk-reward based ({m:g}R); market structure offers no nearer level"})
    for i, t in enumerate(targets, 1):
        t.update(label=f"T{i}", price=_fmt(t["_p"]), risk_reward=round((t["_p"] - entry) / risk, 2))
    return {"entry": entry, "stop": stop, "method": "STRUCTURAL", "risk": risk, "structure": structure, "enough_upside": True, "rr_first": targets[0]["risk_reward"],
            "why": f"Just beyond structural support/resistance ({_fmt(structure)}) with a {buffer_atr:g} ATR buffer; {pct:.1f}% from entry.",
            "targets": [{k: v for k, v in t.items() if k != "_p"} for t in targets]}, None


def gates(horizon: str, setup: str, status: str, plan_: dict[str, Any], scen: dict[str, Any], ind: dict[str, Any], zones: list[dict[str, Any]], recent_lows: list[float],
          band: tuple[float, float], cfg: LevelsV2Config, extra: list[tuple[float, str]] | None = None) -> dict[str, Any]:
    """Status rules on the finished plan. Returns {status, blocked_reason|None, rules[], band_position, entry_kind, entry_label, ready_to_buy}."""
    close, risk, rules = float(ind["close"]), float(plan_["risk"]), []
    lo, hi = band
    out: dict[str, Any] = {"blocked_reason": None}
    if setup == "PULLBACK_ENTRY" and cfg.broken_support_veto:
        bs = broken_support_above(hi, close, recent_lows)
        if bs is not None:
            return {**out, "status": "NO_VALID_SETUP", "blocked_reason": f"The pullback band {_fmt(lo)}-{_fmt(hi)} sits under a broken support at {_fmt(bs)}; price has closed below it.", "rules": ["broken_support"]}
    near = barriers_above(zones, close, extra)
    gap = None if not near else max(0.0, float(near[0]["low"]) - close)
    headroom_blocked = status == "ENTER_NOW" and gap is not None and gap < cfg.headroom_r * risk
    if headroom_blocked:
        status = "WAIT_FOR_ENTRY"
        rules.append("headroom")
        out["headroom_note"] = f"The next resistance ({_fmt(near[0]['low'])}) is only {gap / risk:.1f}R above the last close (minimum {cfg.headroom_r:g}R), so this is not an entry now."
    pos = band_position(close, lo, hi)
    kind = "breakout_trigger" if setup == "BREAKOUT_ENTRY" or pos == "below" else "limit_pullback" if setup == "PULLBACK_ENTRY" else "momentum"
    if kind == "breakout_trigger":
        trigger = float(scen.get("trigger", hi))
        label = f"Breakout trigger: needs a confirming close above {_fmt(trigger)}"
        if setup != "BREAKOUT_ENTRY":
            rules.append("band_above_market")
        out["breakout_trigger"] = _fmt(trigger)
    elif status == "ENTER_NOW":
        label = "In buy band now"
    elif pos == "inside":
        label = "In the band; waiting for momentum confirmation" + (" (headroom limited)" if headroom_blocked else "")
    else:
        label = f"Wait for a pullback into {_fmt(lo)}-{_fmt(hi)}"
    ready = (status == "ENTER_NOW" or (pos == "inside" and kind != "breakout_trigger")) and not headroom_blocked
    return {**out, "status": status, "rules": rules, "band_position": pos, "entry_kind": kind, "entry_label": label, "ready_to_buy": bool(ready)}


# ───────────── split / bonus basis (item 10) ─────────────
def adjust_raw_bars(frame: pd.DataFrame, actions: list[tuple[date, str, float]]) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    """For each split/bonus (ex_date, 'split'|'bonus', ratio): if the bars show the RAW price drop at the ex-date, scale earlier OHLC (and volume) to the post-event basis;
    if they are continuous across it, the provider already adjusted them and nothing is changed. Returns (frame, audit rows)."""
    audit: list[dict[str, Any]] = []
    df = frame.copy()
    for ex, kind, ratio in sorted(actions):
        mult = ratio if kind == "split" else 1.0 + ratio
        if mult <= 0 or abs(mult - 1.0) < 1e-9:
            continue
        pos = df.index.searchsorted(pd.Timestamp(ex))
        if pos <= 0 or pos >= len(df):
            audit.append({"ex_date": str(ex), "kind": kind, "ratio": ratio, "basis": "not_in_window"})
            continue
        gap = float(df["open"].iloc[pos] / df["close"].iloc[pos - 1])
        raw_gap = 1.0 / mult
        if abs(np.log(gap / raw_gap)) < abs(np.log(gap)):          # closer to the raw drop than to no gap: bars are raw
            df.iloc[:pos, df.columns.get_indexer(["open", "high", "low", "close"])] *= raw_gap
            df.iloc[:pos, df.columns.get_loc("volume")] *= mult
            audit.append({"ex_date": str(ex), "kind": kind, "ratio": ratio, "basis": "raw_adjusted", "gap": round(gap, 3)})
        else:
            audit.append({"ex_date": str(ex), "kind": kind, "ratio": ratio, "basis": "already_adjusted", "gap": round(gap, 3)})
    return df, audit
