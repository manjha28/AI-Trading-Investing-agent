"""Buy levels for the research report, mapped from the multi-horizon technical engine (technical_engine.py), for swing, positional and long-term.

No level is computed here: this module only decides whether a horizon's engine plan is shown, and in what shape. Levels appear only when the horizon's
research recommendation is BUY and the daily data is current. Entry can be "enter now" or "wait for entry" (with the planned zone / breakout trigger);
if the engine finds no valid setup the reason is stated and no levels are invented. Decision support, not investment advice; levels are not orders.
"""
from __future__ import annotations

from typing import Any

from app.services.technical_engine import HORIZONS, EngineConfig

_CFG = EngineConfig()
PARAMS: dict[str, dict[str, Any]] = {h: {"max_risk_atr": _CFG.max_risk_atr[h]} for h in HORIZONS}


def _unavailable(status: str, reason: str) -> dict[str, Any]:
    return {"status": status, "reason": reason}


def nearest_pivots(pivots: dict[str, dict[str, str]] | None, price: str) -> tuple[dict[str, str] | None, dict[str, str] | None]:
    """Nearest classic pivot below and above the reference close, across the daily, weekly and monthly sets (comparison only; the prices stay as stored strings)."""
    ref = float(price)
    flat = [(f"{period} {level}", value) for period, levels in (pivots or {}).items() for level, value in levels.items()]
    below = [(n, v) for n, v in flat if float(v) < ref]
    above = [(n, v) for n, v in flat if float(v) > ref]
    support = max(below, key=lambda t: float(t[1]), default=None)
    resistance = min(above, key=lambda t: float(t[1]), default=None)
    return ({"name": support[0], "price": support[1]} if support else None, {"name": resistance[0], "price": resistance[1]} if resistance else None)


def build_levels(technicals: dict[str, Any], assessment: dict[str, Any] | None, engine: dict[str, Any] | None) -> dict[str, Any]:
    horizons = (assessment or {}).get("horizons") or {}
    out: dict[str, Any] = {}
    for horizon in HORIZONS:
        rec = (horizons.get(horizon) or {}).get("recommendation")
        eng = ((engine or {}).get("horizons") or {}).get(horizon) or {}
        if rec != "BUY":
            out[horizon] = _unavailable("not_applicable", f"Levels are offered only for a BUY recommendation (this horizon is {rec or 'unscored'}).")
        elif technicals.get("status") != "available":
            out[horizon] = _unavailable("stale_or_missing_data", technicals.get("message") or "Daily price data is not current, so entry levels are suppressed.")
        elif eng.get("status") != "available":
            out[horizon] = _unavailable("insufficient_history", eng.get("reason") or "The technical engine could not analyse this horizon.")
        elif eng.get("selected_setup") == "NO_VALID_SETUP" or not eng.get("entry") or not eng.get("stop_loss"):
            out[horizon] = _unavailable("no_valid_setup", eng.get("explanation") or "No valid entry setup exists right now.")
        else:
            entry, stop = eng["entry"], eng["stop_loss"]
            pivot_support, pivot_resistance = nearest_pivots(eng["pivots"], eng["current_price"])
            out[horizon] = {
                "status": "available", "entry_status": eng["entry_status"], "selected_setup": eng["selected_setup"], "direction": eng["direction"],
                "technical_score": eng["technical_score"], "market_regime": eng["market_regime"], "reference_close": eng["current_price"],
                "entry_zone": {"low": entry["low"], "high": entry["high"]}, "planned_entry": entry["planned_price"], "breakout_trigger": entry.get("breakout_trigger"),
                "extension_atr": entry.get("extension_atr"), "extension": entry.get("extension"),
                "stop": stop["price"], "invalidation": stop["structural_invalidation"], "stop_method": stop["method"], "stop_reason": stop["reason"],
                "stop_percent": stop["percent"], "risk_atr": stop["atr_units"],
                "targets": eng["targets"], "risk_reward": eng["risk_reward"], "enough_upside": eng["enough_upside"],
                "support_levels": eng["support_levels"], "resistance_levels": eng["resistance_levels"], "pivots": eng["pivots"], "pivot_support": pivot_support, "pivot_resistance": pivot_resistance, "explanation": eng["explanation"],
                "derivation": {"rule": f"Entry setup chosen by the technical engine ({eng['primary_timeframe']} candles): {eng['selected_setup'].replace('_', ' ').lower()}. "
                                       f"Stop = {stop['method'].lower()} ({stop['reason']}) Targets from resistance zones, then risk-reward multiples, each with its basis.",
                               "inputs": {"atr14": eng["atr"]["atr14"], "natr_pct": eng["atr"]["natr"], "last_completed_candle": eng["last_completed_candle"]}, "tick": "0.05"},
            }
    return {"label": "Decision support, not investment advice. Levels are computed from completed candles; they are not orders.", "horizons": out}
