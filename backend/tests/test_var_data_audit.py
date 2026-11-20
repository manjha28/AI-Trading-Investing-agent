"""Data audit / run-status logic. TEST FIXTURE values only; no market data."""
from datetime import date

from app.services.var import service
from app.services.var.loader import MIN_5M_BARS, ContractSeries, UnderlyingData
from app.services.var.vtypes import Contract
from decimal import Decimal as D


def _u(loaded_days, excluded):
    c = Contract("eq-X", "X-EQ", "X", "stock", None, 1, D("0.05"), "equity_cash")
    cs = ContractSeries(c, date(9999, 12, 31))
    for d in loaded_days:
        cs.sessions[d] = []
    u = UnderlyingData("X", "stock", [cs] if loaded_days else [], exclusions=excluded)
    u.sessions_available = len(loaded_days) + len(excluded)
    return u


def days(n, start=date(2026, 1, 1)):
    from datetime import timedelta
    return [start + timedelta(days=i) for i in range(n)]


def test_classify_symbol_covers_every_failure_mode():
    base = {"symbol": "X", "first": None, "last": None}
    assert service.classify_symbol({**base, "exists": False, "sessions_available": 0, "sessions_usable": 0})["status"] == "not_in_storage"
    assert service.classify_symbol({**base, "exists": True, "sessions_available": 0, "sessions_usable": 0})["status"] == "no_bars_in_window"
    r = service.classify_symbol({**base, "exists": True, "sessions_available": 300, "sessions_usable": 0})
    assert r["status"] == "no_usable_sessions" and "15:14" in r["reason"]
    assert service.classify_symbol({**base, "exists": True, "sessions_available": 60, "sessions_usable": 40})["status"] == "ok"
    w = service.classify_symbol({**base, "exists": True, "sessions_available": 12, "sessions_usable": 12})
    assert w["status"] == "ok" and "relative-volume baseline cannot complete" in w["warnings"][0]


def test_zero_usable_sessions_is_never_completed():
    a_none = _u([], [{"date": "2026-01-02", "reason": "truncated_or_incomplete_session"}] * 3).audit()
    assert service.run_status([a_none], 0)[0] == "no_usable_data"
    a_some = _u(days(30), []).audit()
    assert service.run_status([a_some], 0)[0] == "no_usable_data"       # sessions loaded but none replayed (e.g. window outside the data)
    assert service.run_status([a_some], 5000)[0] == "completed"


def test_audit_reports_available_loaded_excluded_with_reasons():
    ex = [{"date": "2026-02-01", "reason": "truncated_or_incomplete_session", "detail": f"60 complete 5m bars < {MIN_5M_BARS}"}] * 4 + [{"date": "2026-03-01", "reason": "off_tick_grid", "detail": "x"}]
    a = _u(days(25), ex).audit(date(2026, 1, 10))
    assert a["sessions_available"] == 30 and a["sessions_loaded"] == 25 and a["sessions_excluded"] == 5
    assert a["excluded_by_reason"] == {"truncated_or_incomplete_session": 4, "off_tick_grid": 1}
    assert a["sessions_loaded_in_window"] == 16                      # Jan 10..Jan 25


def test_relative_volume_baseline_completes_only_with_more_than_twenty_loaded_sessions():
    short = _u(days(20), []).audit()
    assert short["rvol_baseline"]["can_complete"] is False and short["rvol_baseline"]["first_session_with_baseline"] is None
    ok = _u(days(60), []).audit(days(60)[0])
    b = ok["rvol_baseline"]
    assert b["can_complete"] and b["first_session_with_baseline"] == days(60)[20].isoformat() and b["window_sessions_with_baseline"] == 40
    assert _u([], []).audit()["rvol_baseline"]["can_complete"] is False


def test_default_variant_is_the_one_stored_data_can_actually_run():
    from app.services.var.config import VarConfig
    assert VarConfig().variant == "A" and not VarConfig().uses_orderflow       # a default of B guaranteed zero trades on candle data
