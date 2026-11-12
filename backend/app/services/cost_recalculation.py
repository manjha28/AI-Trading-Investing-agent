"""Recalculate transaction charges (and derived net P&L) for stored backtest results using the central cost engine.

NEVER changes gross P&L, executed prices, quantities, timestamps, exit reasons or signals. Only ``charges``, ``cost_breakdown``, ``net_pnl``,
``r_multiple`` and the statistics derived from them move. Original figures are preserved under ``result['cost_recalculation']``.
Limits: slippage is already baked into the stored fills and is left as recorded (re-run the backtest to apply the corrected slippage model);
the stored equity curve (<=1500 points) is shifted by each trade's charge difference at its exit time.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from types import SimpleNamespace
from typing import Any

import pandas as pd

from app.services.intraday_metrics import summarize
from app.services.transaction_costs import compute_charges, schedule_for_market, trade_executions

D = Decimal
CENT = D("0.01")


def recalc_trade(t: dict[str, Any], schedule, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    side, qty = t["side"], D(t["qty"])
    entry_fill, exit_fill = D(t["entry_fill"]), D(t["exit_fill"])
    maker = t["exit_reason"] in ("target", "target_gap")
    bd = compute_charges(trade_executions(side, qty, entry_fill, exit_fill, liquidity_exit="maker" if maker else "taker"), schedule, overrides=overrides)
    sign = D(1) if side == "long" else D(-1)
    net = ((exit_fill - entry_fill) * qty * sign - bd.total).quantize(CENT, ROUND_HALF_UP)
    out = dict(t)
    out.update(charges=str(bd.total.quantize(CENT, ROUND_HALF_UP)), cost_breakdown=bd.as_dict(), net_pnl=str(net), charges_before_recalculation=t["charges"], net_pnl_before_recalculation=t["net_pnl"])
    if t.get("stop_initial") is not None:
        risk = abs(entry_fill - D(t["stop_initial"])) * qty
        out["r_multiple"] = str((net / risk).quantize(CENT, ROUND_HALF_UP)) if risk else None
    return out


def recalculate_result(result: dict[str, Any], definition: dict[str, Any], config: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """Returns a new result dict (input untouched). Only ``single`` results with trades can be recalculated."""
    if "trades" not in result or "metrics" not in result:
        raise ValueError("only single-run results carry trades to recalculate")
    market = definition.get("market", "equity_in")
    schedule = schedule_for_market(market, config.get("cost_profile"))
    overrides = config.get("cost_overrides") or {}
    trades = [recalc_trade(t, schedule, overrides) for t in result["trades"]]
    old_metrics = result["metrics"]
    cap = D(str(config.get("initial_capital", "1000000")))
    # equity curve: shift by (old - new) charges at each trade's exit time
    deltas = sorted(((pd.Timestamp(t["exit_time"]), D(o["charges"]) - D(t["charges"])) for o, t in zip(result["trades"], trades)), key=lambda x: x[0])
    curve = result.get("equity_curve") or []
    et, ev, running, j = [], [], D(0), 0
    for p in curve:
        ts = pd.Timestamp(p["t"])
        while j < len(deltas) and deltas[j][0] <= ts:
            running += deltas[j][1]
            j += 1
        et.append(int(ts.as_unit("ns").value))
        ev.append(float(D(str(p["equity"])) + running))
    tz = "UTC" if market == "crypto" else "Asia/Kolkata"
    fake = SimpleNamespace(trades=trades, cfg=SimpleNamespace(initial_capital=cap), market=market, warnings=list(result.get("warnings", [])), skipped=result.get("skipped_signals", {}),
                           runs=[SimpleNamespace(profile=SimpleNamespace(tz=tz), quality={}, symbol="recalc", frame=pd.DataFrame())], costs=schedule)
    new = summarize(fake, et, ev)
    out = copy.deepcopy(result)
    for key in ("metrics", "equity_curve", "monthly", "by_side", "by_instrument", "by_session", "trade_distribution", "trades"):
        out[key] = new[key]
    out["warnings"] = [w for w in result.get("warnings", [])]
    for k in ("sharpe_ratio", "sortino_ratio", "ratios_note"):
        out["metrics"][k] = new["metrics"][k]
    out["assumptions"] = {**result.get("assumptions", {}), "cost_profile": schedule.describe()}
    out["cost_recalculation"] = {
        "recalculated_at": (now or datetime.now(timezone.utc)).isoformat(), "schedule_id": schedule.id,
        "unchanged": ["gross_pnl", "executed prices", "quantities", "timestamps", "exit reasons", "signals", "slippage as recorded"],
        "old": {k: old_metrics.get(k) for k in ("total_charges", "net_profit", "return_pct", "profit_factor_net", "expectancy_per_trade", "max_drawdown_pct", "win_rate", "winning_trades")},
        "new": {k: out["metrics"].get(k) for k in ("total_charges", "net_profit", "return_pct", "profit_factor_net", "expectancy_per_trade", "max_drawdown_pct", "win_rate", "winning_trades")},
        "note": "Charges recomputed by the central cost engine on the stored executed prices. Slippage in the stored fills is as originally recorded; re-run the backtest to apply the corrected slippage model.",
    }
    return out
