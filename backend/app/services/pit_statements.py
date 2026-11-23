"""Point-in-time statements for the long-term engine (Phase 7b). Everything is dated at the FILING date and nothing later is used.
  * annual figures from quarters: revenue, operating profit / EBITDA, PBT, PAT, EPS, interest, derived tax = the sum of 4 consecutive filed NSE quarters, dated at the filing of the 4th quarter, provenance ``nse_ttm_from_quarters``.
    They stand in as the annual row of a March fiscal year only when no filed annual statement exists for that date.
  * balance sheet: equity, borrowings, cash, total assets from the NSE half-year (March and September) filings, dated at filing; the latest snapshot known at a row's date is attached with its own date.
  * cash flow: CFO from the March (12-month) filing only; a missing CFO stays None (CFO/PAT unknown, never zero).
  * valuation: month-end P/E = the close then / the TTM EPS filed by then; their median over the last 36 month-ends (>= 24 usable) is the own-history P/E.
Pure functions over stored rows (``InstrumentFundamental``-like objects); no data is fetched and no class rule or threshold is touched here."""
from __future__ import annotations

from datetime import date, timedelta
from statistics import median
from typing import Any

import pandas as pd

PROVENANCE = "nse_ttm_from_quarters"
LAG_ANNUAL, LAG_QUARTER = 75, 45
SUM_KEYS = ("revenue", "pat", "eps", "interest", "pbt", "op_profit", "depreciation")


def known_from(r: Any) -> date:
    return r.filed_on or r.period_end + timedelta(days=LAG_ANNUAL if r.period_type == "annual" else LAG_QUARTER)


def _num(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v.get("value") if isinstance(v, dict) else v)
    except (TypeError, ValueError):
        return None


def quarter_dict(r: Any) -> dict[str, Any]:
    ex = r.extra_metrics or {}
    return {"period_end": r.period_end, "known": known_from(r), "revenue": _num(r.revenue), "pat": _num(r.net_income), "eps": _num(r.eps), "interest": _num(ex.get("interest_expense")),
            "pbt": _num(ex.get("profit_before_tax")), "op_profit": _num(ex.get("operating_profit")), "depreciation": _num(ex.get("depreciation"))}


def _consecutive(qs: list[dict[str, Any]]) -> bool:
    return all(60 <= (b["period_end"] - a["period_end"]).days <= 120 for a, b in zip(qs, qs[1:]))


def ttm_series(quarters: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One TTM row per quarter that has 3 filed predecessors: the sum of the four, dated at the 4th quarter's filing. A figure missing from any of the four is None (never summed as zero)."""
    qs = sorted(quarters, key=lambda q: q["period_end"])
    out = []
    for j in range(3, len(qs)):
        w = qs[j - 3:j + 1]
        if not _consecutive(w):
            continue
        row: dict[str, Any] = {"period_end": w[-1]["period_end"], "known": w[-1]["known"], "provenance": PROVENANCE, "quarters": [str(q["period_end"]) for q in w]}
        for k in SUM_KEYS:
            vals = [q.get(k) for q in w]
            row[k] = None if any(v is None for v in vals) else sum(vals)
        row["ebit"] = None if row["pbt"] is None or row["interest"] is None else row["pbt"] + row["interest"]
        row["ebitda"] = None if row["ebit"] is None or row["depreciation"] is None else row["ebit"] + row["depreciation"]
        row["tax_derived"] = None if row["pbt"] is None or row["pat"] is None else row["pbt"] - row["pat"]
        row["op_margin"] = None if row["op_profit"] is None or not row["revenue"] else row["op_profit"] / row["revenue"] * 100
        out.append(row)
    return out


def balance_snapshots(rows: list[Any]) -> list[dict[str, Any]]:
    """Balance sheets from the NSE half-year filings (and any annual balance sheet), dated at filing: equity, debt, cash, assets, fixed assets, CFO (12-month, March only)."""
    out = []
    for r in rows:
        if r.total_equity is None and r.total_assets is None and r.total_debt is None:
            continue
        ex = r.extra_metrics or {}
        out.append({"period_end": r.period_end, "known": known_from(r), "equity": _num(r.total_equity), "debt": _num(r.total_debt), "assets": _num(r.total_assets), "cash": _num(ex.get("cash")),
                    "fixed_assets": _num(ex.get("fixed_assets")), "cfo": _num(r.operating_cash_flow) if r.period_type == "annual" else None, "period_type": r.period_type})
    return sorted(out, key=lambda s: s["known"])


def latest_snapshot(snaps: list[dict[str, Any]], as_of: date) -> dict[str, Any] | None:
    ok = [s for s in snaps if s["known"] <= as_of]
    return ok[-1] if ok else None


def annual_series(annual_engine_rows: list[dict[str, Any]], ttm: list[dict[str, Any]], snaps: list[dict[str, Any]], as_of: date) -> list[dict[str, Any]]:
    """Filed annual rows (already known at ``as_of``) plus, for each March fiscal year end with no filed annual row, the TTM-from-quarters row (known at ``as_of``), with the latest known balance sheet attached.
    Sorted oldest first; every synthesized row carries ``provenance``."""
    have = {r["period_end"] for r in annual_engine_rows}
    rows = list(annual_engine_rows)
    for t in ttm:
        if t["period_end"].month != 3 or t["period_end"] in have or t["known"] > as_of:
            continue
        exact = next((x for x in snaps if x["period_end"] == t["period_end"] and x["known"] <= as_of), None)           # the fiscal year's own balance sheet once it is filed, else the latest earlier one
        s = exact or latest_snapshot(snaps, t["known"])
        row = {"period_end": t["period_end"], "revenue": t["revenue"], "pat": t["pat"], "eps": t["eps"], "op_margin": t["op_margin"], "interest": t["interest"], "pbt": t["pbt"], "ebit": t["ebit"], "op_profit": t["op_profit"],
               "assets": None, "equity": None, "debt": None, "cfo": None, "shares": None, "cash": None, "fixed_assets": None, "provenance": PROVENANCE}
        if s:
            row.update(equity=s["equity"], debt=s["debt"], assets=s["assets"], cash=s["cash"], fixed_assets=s["fixed_assets"], balance_as_of=str(s["period_end"]))
            if s["period_end"] == t["period_end"]:
                row["cfo"] = s["cfo"]
        rows.append(row)
    return sorted(rows, key=lambda r: r["period_end"])


def pe_history(daily_close: pd.Series, ttm: list[dict[str, Any]], as_of: date, months: int = 36) -> tuple[float | None, int]:
    """Median month-end P/E over the last ``months`` month-ends: the close then over the latest TTM EPS filed by then (positive only). Returns (median or None, usable months); None below 24 usable months."""
    pes = []
    for me in pd.date_range(end=pd.Timestamp(as_of), periods=months, freq="ME"):
        px = daily_close.loc[:me]
        if len(px) == 0 or (me - px.index[-1]).days > 7:
            continue
        known = [t for t in ttm if t["known"] <= me.date() and t.get("eps")]
        if not known or known[-1]["eps"] <= 0:
            continue
        pes.append(float(px.iloc[-1]) / known[-1]["eps"])
    return (median(pes) if len(pes) >= 24 else None), len(pes)
