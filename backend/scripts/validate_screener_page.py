"""Validate the Screener importer against a company page YOU saved from your own browser.

    cd backend && python scripts/validate_screener_page.py ../diagnostics/screener_pages/BEL.html [...]

Offline only: reads local files, never touches the network or your real database (an in-memory SQLite is used).
Prints every extracted value in the page's own units next to the stored INR value so you can compare against the
saved page, then runs internal accounting checks, a gap-fill/conflict run on partial rows, and an idempotency run.
"""
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.db import Base  # noqa: E402
from app.models import Instrument, InstrumentFundamental  # noqa: E402
from app.services import screener_import as si  # noqa: E402

CR = si.CRORE
NOW = datetime.now(timezone.utc)


def show(parsed: si.ParsedScreener) -> None:
    print(f"basis={parsed.basis}  periods={len(parsed.periods)}  (values: INR | page units Rs.Cr shown for money)")
    for (kind, end), period in sorted(parsed.periods.items(), key=lambda i: (i[0][0], i[0][1])):
        print(f"\n[{kind} {end}]")
        for name, value in sorted(period.fields.items()):
            money = name not in {"eps", "operating_margin"}
            page = f"  = {value / CR:,.2f} Cr" if money else ""
            print(f"  {name:22s} {value:>26,.2f}{page}{'  (derived)' if name in period.derived else ''}")
        for name, value in sorted(period.extras.items()):
            print(f"  · {name:20s} {value:>26,.2f}")


def checks(parsed: si.ParsedScreener) -> list[str]:
    problems = []
    for (kind, end), p in parsed.periods.items():
        f = p.fields
        if {"total_equity", "total_liabilities", "total_assets"} <= f.keys():
            gap = f["total_equity"] + f["total_liabilities"] - f["total_assets"]
            if abs(gap) > CR:  # > Rs.1 Cr
                problems.append(f"{kind} {end}: equity + liabilities - assets = {gap / CR:,.2f} Cr (identity should be ~0; minority interest / other items?)")
        if "operating_profit" in p.extras and "revenue" in f and f["revenue"] and "operating_margin" in f:
            implied = p.extras["operating_profit"] / f["revenue"] * 100
            if abs(implied - f["operating_margin"]) > Decimal("1.5"):
                problems.append(f"{kind} {end}: OPM% {f['operating_margin']} vs operating_profit/revenue {implied:.1f}%")
        if "total_debt" in f and "total_equity" in f and f["total_equity"] <= 0:
            problems.append(f"{kind} {end}: non-positive equity {f['total_equity'] / CR:,.2f} Cr (ratios will be blocked)")
    return problems


def run(path: Path) -> int:
    print("=" * 100, f"\n{path.name}")
    parsed = si.parse_screener_html(path.read_text(encoding="utf-8", errors="replace"))
    show(parsed)
    issues = checks(parsed)
    print("\nINTERNAL CHECKS:", "none failed" if not issues else "")
    for issue in issues:
        print("  !", issue)

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        inst = Instrument(symbol=path.stem.upper(), display_name=path.stem, asset_class="equity", venue="NSE", currency="INR",
                          country="IN", timezone="Asia/Kolkata", created_at=NOW)
        db.add(inst)
        db.flush()
        # Seed ONE partial provider row for the newest annual period: revenue present (deliberately 3% off to force a conflict), rest NULL.
        newest = max(e for k, e in parsed.periods if k == "annual")
        seed = parsed.periods[("annual", newest)].fields.get("revenue")
        db.add(InstrumentFundamental(id=uuid4(), instrument_id=inst.id, period_type="annual", period_end=newest, currency="INR",
                                     source="yfinance", fetched_at=NOW, revenue=(seed * Decimal("1.03")) if seed else None))
        db.flush()
        first = si.apply_gap_fill(db, inst, parsed, NOW)
        print(f"\nGAP FILL: created={len(first.created_periods)} filled={first.filled.get(f'annual {newest}', [])} conflicts={first.conflicts} skipped={first.skipped}")
        before = {(r.period_type, r.period_end): [getattr(r, f) for f in si.CANONICAL_FIELDS] for r in db.scalars(select(InstrumentFundamental))}
        extras_first = {(r.period_type, r.period_end): (r.field_sources, r.extra_metrics) for r in db.scalars(select(InstrumentFundamental))}
        again = si.apply_gap_fill(db, inst, parsed, NOW)
        after = {(r.period_type, r.period_end): [getattr(r, f) for f in si.CANONICAL_FIELDS] for r in db.scalars(select(InstrumentFundamental))}
        stored_identical = extras_first == {(r.period_type, r.period_end): (r.field_sources, r.extra_metrics) for r in db.scalars(select(InstrumentFundamental))}
        idem = stored_identical and before == after and not again.created_periods and not again.filled  # an unresolved conflict is re-reported, never re-written
        print(f"IDEMPOTENT RE-IMPORT: {'yes' if idem else 'NO'}")
        nulls = {f: sum(1 for v in before.values() if v[si.CANONICAL_FIELDS.index(f)] is None) for f in si.CANONICAL_FIELDS}
        print("rows still NULL per field (never zero-filled):", {k: v for k, v in nulls.items() if v})
        return 0 if (idem and not issues) else 1


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    sys.exit(max(run(Path(arg)) for arg in sys.argv[1:]))
