"""Point-in-time statements from filed quarters and half-year balance sheets. Test data: hand-made rows."""
from datetime import date, timedelta
from types import SimpleNamespace as NS

import pandas as pd

from app.services import pit_statements as ps


def q(end, filed, rev, pat, eps, *, interest=1, pbt=None, op=None, dep=1):
    return NS(period_type="quarterly", period_end=end, filed_on=filed, revenue=rev, net_income=pat, eps=eps, total_equity=None, total_assets=None, total_debt=None, operating_cash_flow=None,
              extra_metrics={"interest_expense": interest, "profit_before_tax": pbt if pbt is not None else pat + 2, "operating_profit": op if op is not None else rev * 0.2, "depreciation": dep})


QENDS = [date(2024, 6, 30), date(2024, 9, 30), date(2024, 12, 31), date(2025, 3, 31), date(2025, 6, 30)]
QROWS = [q(e, e + timedelta(days=40), 100 + 10 * i, 10 + i, 1.0 + 0.1 * i) for i, e in enumerate(QENDS)]


def test_ttm_is_the_sum_of_four_consecutive_filed_quarters_dated_at_the_fourth_filing():
    t = ps.ttm_series([ps.quarter_dict(r) for r in QROWS])
    assert len(t) == 2 and t[0]["period_end"] == date(2025, 3, 31) and t[0]["known"] == date(2025, 5, 10)
    assert t[0]["revenue"] == 100 + 110 + 120 + 130 and t[0]["pat"] == 10 + 11 + 12 + 13 and round(t[0]["eps"], 6) == 1.0 + 1.1 + 1.2 + 1.3
    assert t[0]["provenance"] == "nse_ttm_from_quarters" and t[0]["ebit"] == t[0]["pbt"] + t[0]["interest"] and t[0]["tax_derived"] == t[0]["pbt"] - t[0]["pat"] and t[0]["ebitda"] == t[0]["ebit"] + 4


def test_a_missing_figure_in_any_quarter_makes_the_ttm_figure_unknown_not_a_smaller_sum():
    rows = [ps.quarter_dict(r) for r in QROWS]
    rows[1]["eps"] = None
    t = ps.ttm_series(rows)
    assert t[0]["eps"] is None and t[0]["revenue"] == 460 and t[1]["eps"] is None


def test_a_gap_in_the_quarters_breaks_the_window():
    rows = [ps.quarter_dict(r) for i, r in enumerate(QROWS) if i != 2]
    assert ps.ttm_series(rows) == []


def test_annual_series_uses_ttm_only_for_a_march_year_without_a_filed_annual_and_never_before_it_is_known():
    ttm = ps.ttm_series([ps.quarter_dict(r) for r in QROWS])
    filed_fy24 = {"period_end": date(2024, 3, 31), "revenue": 400.0}
    assert ps.annual_series([filed_fy24], ttm, [], date(2025, 5, 1)) == [filed_fy24]                       # FY25 TTM is known only on 2025-05-10
    rows = ps.annual_series([filed_fy24], ttm, [], date(2025, 5, 11))
    assert [r["period_end"] for r in rows] == [date(2024, 3, 31), date(2025, 3, 31)] and rows[1]["provenance"] == "nse_ttm_from_quarters" and rows[1]["cfo"] is None
    both = ps.annual_series([filed_fy24, {"period_end": date(2025, 3, 31), "revenue": 999.0}], ttm, [], date(2025, 8, 1))
    assert [r["revenue"] for r in both] == [400.0, 999.0]                                                    # a filed annual always wins


def test_balance_snapshot_is_the_latest_known_and_cfo_only_comes_from_a_march_year():
    sep = NS(period_type="quarterly", period_end=date(2024, 9, 30), filed_on=date(2024, 11, 8), total_equity=500, total_assets=900, total_debt=150, operating_cash_flow=None, extra_metrics={"cash": {"value": "40"}, "cfo_6m": {"value": "60"}})
    mar = NS(period_type="annual", period_end=date(2025, 3, 31), filed_on=date(2025, 5, 12), total_equity=550, total_assets=950, total_debt=140, operating_cash_flow=130, extra_metrics={"cash": {"value": "45"}})
    snaps = ps.balance_snapshots([mar, sep])
    assert [s["period_end"] for s in snaps] == [date(2024, 9, 30), date(2025, 3, 31)] and snaps[0]["cfo"] is None and snaps[1]["cfo"] == 130.0
    assert ps.latest_snapshot(snaps, date(2025, 1, 1))["equity"] == 500 and ps.latest_snapshot(snaps, date(2024, 1, 1)) is None
    ttm = ps.ttm_series([ps.quarter_dict(r) for r in QROWS])
    row = ps.annual_series([], ttm, snaps, date(2025, 6, 1))[0]
    assert row["equity"] == 550 and row["cfo"] == 130.0 and row["balance_as_of"] == "2025-03-31"          # the March balance sheet (known 2025-05-12) is attached once it is filed
    early = ps.annual_series([], ttm, snaps, date(2025, 5, 11))[0]
    assert early["equity"] == 500 and early["cfo"] is None and early["balance_as_of"] == "2024-09-30"       # before it is filed: the September one, no CFO (never the 6-month figure)
