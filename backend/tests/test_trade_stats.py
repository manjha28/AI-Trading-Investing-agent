"""Shared statistics + suppression. Expected values computed by hand. TEST FIXTURE data."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from app.services import trade_stats as ts


def test_mean_se_t_and_ci_by_hand():
    # values 1..5: mean 3, sample sd = sqrt(2.5) = 1.58114, se = 0.70711, t = 4.24264, df 4 -> t_crit 2.776 -> CI 3 +/- 1.96 = [1.037, 4.963]
    m = ts.mean_stats([1, 2, 3, 4, 5])
    assert m["n"] == 5 and m["mean"] == 3 and m["se"] == pytest.approx(0.70711, abs=1e-4) and m["t_stat"] == pytest.approx(4.24264, abs=1e-4)
    assert m["ci95"] == [pytest.approx(3 - 2.776 * 0.70711, abs=1e-3), pytest.approx(3 + 2.776 * 0.70711, abs=1e-3)]
    assert ts.mean_stats([5])["se"] is None and ts.mean_stats([])["n"] == 0


def test_t_critical_values():
    assert ts.t_crit_975(11) == 2.201 and ts.t_crit_975(99) == pytest.approx(1.984, abs=2e-3) and ts.t_crit_975(100000) == 1.96


def _trades(n, net=D("10")):
    t0 = datetime(2026, 11, 2, 4, tzinfo=timezone.utc)
    return [{"gross_pnl": net + D("5"), "net_pnl": net, "charges": D("5"), "turnover": D("1000"), "r_multiple": 0.5, "entry_ts": t0 + timedelta(minutes=i), "exit_ts": t0 + timedelta(minutes=i + 5)} for i in range(n)]


def test_claims_are_suppressed_below_100_trades_and_released_at_100():
    small = ts.net_gross_block(_trades(12), D("1000000"), n_configs=7)
    assert small["performance"].startswith("suppressed") and small["sample"]["state"] == "insufficient_sample" and small["trades"] == 12
    assert "win_rate_net_pct" not in small and small["configurations_evaluated"] == 7 and "diagnostics_only" in small
    big = ts.net_gross_block(_trades(100), D("1000000"), n_configs=7)
    assert big["sample"]["state"] == "ok" and big["headline"] == "net" and big["net_pnl"] == "1000" and big["gross_pnl"] == "1500" and big["total_charges"] == "500"
    assert big["net_per_trade_stats"]["n"] == 100 and big["configurations_evaluated"] == 7


def test_gross_minus_charges_equals_net_in_aggregate():
    b = ts.net_gross_block(_trades(100), D("1000000"))
    assert D(b["gross_pnl"]) - D(b["total_charges"]) == D(b["net_pnl"])
