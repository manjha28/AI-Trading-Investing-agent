"""Repeatable VAR data-validation report (read-only). Writes diagnostics/var_data_validation.md, replacing the previous one.

Run from backend/:  python scripts/var_data_validation.py [UNDERLYING ...]
States what the stored candles can and cannot support; never reports order-flow capability the store does not have."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.config import settings  # noqa: E402
from app.services.var import service  # noqa: E402
from app.services.var.loader import load_underlying  # noqa: E402


def main(unders: list[str]) -> Path:
    out = Path(__file__).resolve().parents[2] / "diagnostics" / "var_data_validation.md"
    lines = ["# VAR data validation (generated)", ""]
    with Session(create_engine(settings.database_url)) as db:
        caps = service.data_capabilities(db)
        lines += ["## Capability", f"- data level: **{caps['data_level']}**", f"- order flow: {caps['order_flow']}", f"- volume unit: {caps['volume_unit']}",
                  f"- tick size: {caps['tick_size_unit']}", f"- lot size: {caps['lot_size_note']}", f"- session: {caps['session_note']}", "",
                  "Supported: " + "; ".join(caps["supports"]), "", "Not supported: " + "; ".join(caps["does_not_support"]), "", "## Per contract (5m aggregation from stored 1m candles)", "",
                  "| underlying | contract | lot | tick (₹) | sessions | first | last | sessions with <75 bars | dropped 5m groups | off-tick bars |", "|---|---|---|---|---|---|---|---|---|---|"]
        for u in unders:
            d = load_underlying(db, u)
            for n in d.notes:
                lines.append(f"| {u} | NOTE: {n} | | | | | | | | |")
            for cs in d.series:
                short = sum(1 for dd in cs.sessions if len(cs.sessions[dd]) < 75)
                drop = sum(c["dropped_incomplete_groups"] for c in cs.coverage.values())
                off = sum(c["off_tick_bars"] for c in cs.coverage.values())
                lines.append(f"| {u} | {cs.contract.symbol} | {cs.contract.lot_size} | {cs.contract.tick_size} | {len(cs.sessions)} | {min(cs.sessions)} | {max(cs.sessions)} | {short} | {drop} | {off} |")
    out.parent.mkdir(exist_ok=True)
    out.write_text("\n".join(lines) + "\n")
    return out


if __name__ == "__main__":
    print(main(sys.argv[1:] or ["NIFTY", "BANKNIFTY"]))
