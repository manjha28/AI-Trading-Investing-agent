"""Previous-vs-corrected cost comparison for the original 48-session NIFTY/BANKNIFTY variant A run. Writes diagnostics/phase1_cost_comparison.json.

Same trades, same config; ONLY the cost model differs. The retired model is re-applied to each trade's actual orders (side + turnover) with the
exact formula that produced diagnostics/var_real_data_backtest_v1_original_cost_model.json, and the reconstruction is checked against that file's
saved total charges BEFORE any delta is reported. The original file is never modified.

Run from backend/:  python scripts/phase1_compare.py"""
from __future__ import annotations

import json
import sys
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.config import settings  # noqa: E402
from app.services import indian_charges as ic  # noqa: E402
from app.services.trade_stats import mean_stats  # noqa: E402
from app.services.var.backtest import instrument_data_from_underlying, run_backtest  # noqa: E402
from app.services.var.config import VarConfig  # noqa: E402
from app.services.var.loader import load_underlying  # noqa: E402

D = Decimal
ROOT = Path(__file__).resolve().parents[2] / "diagnostics"
CAP = D("2000000")


def old_model(orders: list[dict]) -> dict[str, D]:
    """The retired VAR calculator: brokerage min(Rs20, 0.03% x turnover) per order, STT 0.05% sell (undated), exchange 0.00183%, SEBI 0.0001%,
    stamp 0.002% buy, GST 18% on brokerage+exchange+SEBI (no IPFT), summed per trade then rounded to paise only (no rupee rounding)."""
    b = stt = ex = sebi = stamp = D(0)
    for o in orders:
        t, side = D(o["turnover"]), o["side"]
        b += min(D(20), t * D("0.03") / 100)
        ex += t * D("0.00183") / 100
        sebi += t * D("0.0001") / 100
        if side == "sell":
            stt += t * D("0.05") / 100
        else:
            stamp += t * D("0.002") / 100
    gst = (b + ex + sebi) * D("0.18")
    q = lambda x: x.quantize(D("0.01"), rounding=ROUND_HALF_UP)  # noqa: E731
    return {"brokerage": q(b), "exchange": q(ex), "ipft": D(0), "sebi": q(sebi), "stt": q(stt), "stamp": q(stamp), "gst": q(gst)}


def main() -> None:
    with Session(create_engine(settings.database_url)) as db:
        data = [instrument_data_from_underlying(load_underlying(db, u)) for u in ("NIFTY", "BANKNIFTY")]
    r = run_backtest(data, VarConfig(variant="A"), CAP)
    old_file = json.loads((ROOT / "var_real_data_backtest_v1_original_cost_model.json").read_text())["A_fixed_partial"]["summary"]
    keys = ("brokerage", "exchange", "ipft", "sebi", "stt", "stamp", "gst")
    old_tot = {k: D(0) for k in keys}
    new_tot = {k: D(0) for k in keys}
    rows = []
    sel = {}
    for t in r.trades:
        o = old_model(t["charge_orders"])
        for k in keys:
            old_tot[k] += o[k]
        n = {"brokerage": t["brokerage"], "exchange": t["exchange_charge"], "ipft": t["ipft"], "sebi": t["sebi"], "stt": t["stt"], "stamp": t["stamp_duty"], "gst": t["gst"]}
        for k in keys:
            new_tot[k] += n[k]
        old_c = sum(o.values(), D(0))
        sel[t["entry_ts"].isoformat()] = old_c
        rows.append({"instrument": t["instrument"], "setup": t["setup"], "date": t["date"], "gross_pnl": str(t["gross_pnl"]), "old_charges": str(old_c), "new_charges": str(t["charges"]),
                     "old_net": str(t["gross_pnl"] - old_c), "new_net": str(t["net_pnl"]), "sell_dates_schedule": t["charge_orders"][0]["rates"]["stt"]["rate"]})
    old_total = sum(old_tot.values(), D(0))
    new_total = sum(new_tot.values(), D(0))
    saved_total = D(str(old_file["charges"]))
    gross = sum((t["gross_pnl"] for t in r.trades), D(0))
    pf = lambda xs: float(sum((x for x in xs if x > 0), D(0)) / -sum((x for x in xs if x <= 0), D(0))) if any(x <= 0 for x in xs) else None  # noqa: E731
    old_nets = [t["gross_pnl"] - sel[t["entry_ts"].isoformat()] for t in r.trades]
    new_nets = [t["net_pnl"] for t in r.trades]
    out = {
        "scope": "NIFTY + BANKNIFTY futures, variant A, both engines, default config, 48 sessions; only the cost model differs",
        "reconstruction_check": {"saved_original_total_charges": str(saved_total), "reconstructed_with_retired_formula": str(old_total), "difference": str(old_total - saved_total),
                                 "faithful": abs(old_total - saved_total) < D("1.00")},
        "trade_count": {"previous": old_file["trades"], "corrected": len(r.trades)},
        "gross_pnl": {"previous_saved": old_file["gross_pnl"], "corrected": str(gross)},
        "charges_by_component": {k: {"previous_reconstructed": str(old_tot[k]), "corrected": str(new_tot[k]), "delta": str(new_tot[k] - old_tot[k])} for k in keys},
        "total_charges": {"previous": str(old_total), "corrected": str(new_total), "delta": str(new_total - old_total)},
        "net_pnl": {"previous": str(gross - old_total), "corrected": str(gross - new_total)},
        "net_profit_factor": {"previous": pf(old_nets), "corrected": pf(new_nets)},
        "net_expectancy_per_trade": {"previous": str(((gross - old_total) / len(r.trades)).quantize(D("0.01"))), "corrected": str(((gross - new_total) / len(r.trades)).quantize(D("0.01")))},
        "net_per_trade_stats_corrected": mean_stats(new_nets),
        "stt_schedule_selected_for_all_trades": sorted({row["sell_dates_schedule"] for row in rows}),
        "schedule_version": ic.SCHEDULE_VERSION, "unverified_components": sorted({c for t in r.trades for c in t["unverified_components"]}),
        "claim_status": "NOT a performance claim: 12 trades (< 100). The table isolates the accounting effect of the cost model.", "trades": rows,
    }
    (ROOT / "phase1_cost_comparison.json").write_text(json.dumps(out, indent=1, default=str))
    print(json.dumps({k: v for k, v in out.items() if k != "trades"}, indent=1, default=str))


if __name__ == "__main__":
    main()
