"""Multi-perpetual scanner, paper only: signals from many contracts go into ONE queue and are sized by the owner's risk rules.

Per trade: risk 0.5% of the wallet, effective leverage capped at 5x, several positions may be open at once but the total open risk is capped (default 2% of wallet).
Everything is HYPOTHETICAL and never mixed with real P&L. There is no order path here and no `execute_live` field anywhere in the scanner's data model.
Money is Decimal. The wallet is a fixed paper balance (no compounding).

Conflict to decide later: in LIVE mode the owner's rule "balance in use means paper" limits trading to one position at a time per wallet, which is stricter than this
paper scanner's several-open-positions-with-a-total-risk-cap. Paper results with several open positions therefore overstate what live could do under that rule."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_FLOOR, Decimal
from typing import Any

D = Decimal
LABEL = "Hypothetical paper scanner on completed Delta candles. Decision support, not investment advice."


@dataclass(frozen=True)
class RiskRules:
    wallet: Decimal = D(1000)
    risk_pct: Decimal = D("0.005")
    leverage_cap: Decimal = D(5)
    total_risk_cap_pct: Decimal = D("0.02")


def _t(value: str) -> datetime:
    return datetime.fromisoformat(value)


def simulate(per_symbol: dict[str, dict[str, Any]], rules: RiskRules) -> dict[str, Any]:
    """per_symbol: {symbol: {"contract_value": Decimal, "trades": [trade dicts replayed with 1 contract]}}. Returns accepted (sized) trades, rejected signals with reasons,
    per-symbol and total net P&L, and the full signal log in time order."""
    signals: list[dict[str, Any]] = []
    for symbol, data in per_symbol.items():
        for t in data["trades"]:
            if t.get("exit_time") is None:
                continue
            signals.append({"symbol": symbol, "cv": data["contract_value"], "t": t})
    signals.sort(key=lambda s: (_t(s["t"]["entry_time"]), s["symbol"]))
    accepted: list[dict[str, Any]] = []
    log: list[dict[str, Any]] = []
    open_pos: list[dict[str, Any]] = []
    budget = rules.wallet * rules.total_risk_cap_pct
    for s in signals:
        t, cv, sym = s["t"], s["cv"], s["symbol"]
        entry_at, exit_at = _t(t["entry_time"]), _t(t["exit_time"])
        open_pos = [p for p in open_pos if p["exit"] > entry_at]            # positions that have closed by now free their risk
        row = {"symbol": sym, "side": t["side"], "signal_time": t["signal_time"], "entry_time": t["entry_time"]}
        entry, stop = D(t["entry_fill"]), D(t["stop_initial"])
        unit_risk = abs(entry - stop) * cv
        if unit_risk <= 0:
            log.append({**row, "taken": False, "reason": "no stop distance"})
            continue
        if any(p["symbol"] == sym for p in open_pos):
            log.append({**row, "taken": False, "reason": "already in a position in this contract"})
            continue
        qty = int((rules.wallet * rules.risk_pct / unit_risk).to_integral_value(rounding=ROUND_FLOOR))
        qty_lev = int((rules.wallet * rules.leverage_cap / (entry * cv)).to_integral_value(rounding=ROUND_FLOOR))
        capped = qty > qty_lev
        qty = min(qty, qty_lev)
        if qty < 1:
            log.append({**row, "taken": False, "reason": "risk budget or 5x cap is below one contract"})
            continue
        risk = unit_risk * qty
        used = sum((p["risk"] for p in open_pos), D(0))
        if used + risk > budget:
            log.append({**row, "taken": False, "reason": f"total risk cap: {used + risk:.2f} would exceed {budget:.2f}"})
            continue
        net = D(t["net_pnl"]) * qty
        open_pos.append({"symbol": sym, "risk": risk, "exit": exit_at})
        trade = {**t, "contracts": qty, "risk_usd": str(risk.quantize(D("0.01"))), "net_pnl": str(net), "leverage_capped": capped}
        accepted.append(trade)
        log.append({**row, "taken": True, "reason": "taken" + (" (size cut by the 5x leverage cap)" if capped else ""), "contracts": qty})
    by_symbol: dict[str, dict[str, Any]] = {}
    for tr in accepted:
        b = by_symbol.setdefault(tr["symbol"], {"trades": 0, "net_pnl": D(0)})
        b["trades"] += 1
        b["net_pnl"] += D(tr["net_pnl"])
    total = sum((b["net_pnl"] for b in by_symbol.values()), D(0))
    return {"label": LABEL, "paper_only": True, "rules": {"wallet": str(rules.wallet), "risk_pct": str(rules.risk_pct), "leverage_cap": str(rules.leverage_cap), "total_risk_cap_pct": str(rules.total_risk_cap_pct)},
            "trades": accepted, "per_symbol": {k: {"trades": v["trades"], "net_pnl": str(v["net_pnl"])} for k, v in sorted(by_symbol.items())}, "total_net_pnl": str(total),
            "signals_seen": len(signals), "signals_taken": len(accepted), "signal_log": log[-300:],
            "live_note": "Live mode would still be limited by the 'balance in use means paper' rule (one position at a time); see the module note."}
