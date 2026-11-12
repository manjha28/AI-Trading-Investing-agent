"""Recalculate stored intraday backtest charges with the central cost engine (dry run by default).

    python scripts/recalculate_backtest_costs.py --out ../diagnostics/cost_recalculation.md          # report only
    python scripts/recalculate_backtest_costs.py --apply                                            # write corrected results (originals kept inside result)
"""
import argparse
import sys
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.db import engine  # noqa: E402
from app.models.strategies import BacktestRun  # noqa: E402
from app.services.cost_recalculation import recalculate_result  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--out")
    args = ap.parse_args()
    lines = ["# Backtest charge recalculation", "", "| run | trades | old charges | new charges | old net | new net | old charges/turnover | new charges/turnover |", "|---|---:|---:|---:|---:|---:|---:|---:|"]
    detail = []
    with Session(engine) as db:
        for run in db.scalars(select(BacktestRun).where(BacktestRun.status == "completed", BacktestRun.kind == "single").order_by(BacktestRun.created_at)):
            res = run.result or {}
            if not res.get("trades") or "cost_recalculation" in res:
                continue
            new = recalculate_result(res, run.definition_snapshot, {k: v for k, v in run.config.items() if k != "_options"})
            o, n = res["metrics"], new["metrics"]
            turnover = sum((D(t["entry_fill"]) + D(t["exit_fill"])) * D(t["qty"]) for t in res["trades"])
            lines.append(f"| {str(run.id)[:8]} | {o['total_trades']} | {o['total_charges']} | {n['total_charges']} | {o['net_profit']} | {n['net_profit']} | "
                         f"{D(o['total_charges']) / turnover * 100:.4f}% | {n['charges_pct_of_turnover']}% |")
            detail.append((run, res, new))
            if args.apply:
                run.result = new
        if args.apply:
            db.commit()
    text = "\n".join(lines) + f"\n\nmode: {'APPLIED' if args.apply else 'dry run'}; {len(detail)} run(s)\n"
    print(text)
    if args.out:
        Path(args.out).write_text(text)


if __name__ == "__main__":
    main()
