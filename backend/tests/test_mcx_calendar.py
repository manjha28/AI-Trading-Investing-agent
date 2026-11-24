from datetime import datetime, timezone

from app.services.mcx_calendar import session_state


def ist(y, m, d, h, mi):  # IST wall clock -> UTC instant
    from datetime import timedelta
    return datetime(y, m, d, h, mi, tzinfo=timezone.utc) - timedelta(hours=5, minutes=30)


def test_open_on_a_normal_weekday_and_in_the_us_winter_closes_at_2355():
    assert session_state(ist(2026, 11, 25, 9, 5))["open"] is True            # Wed 09:05
    assert session_state(ist(2026, 11, 25, 23, 40))["open"] is True          # after 23:30, before 23:55 (US standard time)
    assert session_state(ist(2026, 11, 26, 0, 1))["open"] is False


def test_closed_states_always_carry_a_reason_and_the_next_open():
    sat = session_state(ist(2026, 11, 28, 12, 0))
    assert sat["open"] is False and sat["reason"] == "weekend" and sat["next_open"].startswith("2026-11-30T09:00")
    assert "before the 09:00" in session_state(ist(2026, 11, 25, 8, 30))["reason"]


def test_guru_nanak_jayanti_closes_the_morning_but_not_the_evening_and_christmas_closes_the_day():
    morning = session_state(ist(2026, 11, 24, 9, 5))
    assert morning["open"] is False and "Guru Nanak Jayanti" in morning["reason"] and "17:00" in morning["reason"]
    assert session_state(ist(2026, 11, 24, 17, 5))["open"] is True
    assert session_state(ist(2026, 12, 25, 18, 0))["open"] is False


def test_last_close_quotes_use_the_price_dhan_reports_before_the_first_trade_and_never_a_zero(monkeypatch):
    from decimal import Decimal

    from app import broker_adapters as ba
    # TEST DATA shaped like Dhan's reply before the session trades: last_price is the previous close, open/high/low are 0, no trade time
    payload = {"status": "success", "data": {"MCX_COMM": {"578632": {"last_price": 323.8, "ohlc": {"open": 0, "close": 323.8, "high": 0, "low": 0}, "last_trade_time": ""},
                                                          "578633": {"last_price": 0, "ohlc": {"open": 0, "close": 0, "high": 0, "low": 0}}}}}
    monkeypatch.setattr(ba, "_request", lambda *a, **k: payload)
    monkeypatch.setattr(ba, "_wait_for_dhan_market_data", lambda: None)
    adapter = ba.DhanAdapter()
    monkeypatch.setattr(adapter, "_check_market_data_payload", lambda p: None)
    monkeypatch.setattr(ba.DhanAdapter, "price_tick_for", staticmethod(lambda seg, sid: Decimal("0.1")))
    got = adapter.get_last_close_quotes("t", {"client_id": "c"}, "MCX_COMM", ["578632", "578633"])
    assert got == {"578632": (Decimal("323.8"), Decimal("323.8"))}          # the unpriced contract is absent, not zero


# ---- A3: last-session figures while MCX is closed (all values below are TEST DATA) ----
def test_closed_states_weekend_and_after_close_in_both_dst_regimes_and_open_state():
    from datetime import datetime, timezone

    from app.services import mcx_calendar as cal
    sat = datetime(2026, 11, 21, 8, 0, tzinfo=timezone.utc)                       # Saturday
    assert cal.session_state(sat)["open"] is False and cal.session_state(sat)["reason"] == "weekend"
    winter_after = datetime(2026, 11, 25, 18, 26, tzinfo=timezone.utc)              # 23:56 IST, US winter: closes 23:55
    assert cal.session_state(winter_after)["open"] is False
    winter_before = datetime(2026, 11, 25, 18, 20, tzinfo=timezone.utc)             # 23:50 IST still open
    assert cal.session_state(winter_before)["open"] is True
    summer_after = datetime(2026, 7, 7, 18, 1, tzinfo=timezone.utc)                 # 23:31 IST, US summer: closes 23:30
    assert cal.session_state(summer_after)["open"] is False


def test_closed_tick_recomputes_change_in_decimal_and_keeps_unknowns_unknown():
    from app.services.mcx_last_session import closed_tick
    t = closed_tick("mcx:1", "GOLD", {"ltp": "100.50", "previous_close": "100.00", "open": "99", "high": "101", "low": "98", "volume": "10", "oi": "5", "tick_time": None, "session_date": "2026-11-24"}, source="x")
    assert t["change"] == "0.50" and t["change_pct"] == "0.50" and t["oi"] == "5" and t["last_close"] is True
    u = closed_tick("mcx:2", "SILVER", {"ltp": "50", "previous_close": None}, source="x")
    assert u["change"] is None and u["open"] is None and u["volume"] is None             # never zero


def test_snapshot_is_stored_throttled_and_loaded_back():
    from datetime import date

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from app.models import Base
    from app.models.mcx_snapshot import McxLastSnapshot
    from app.services import mcx_last_session as ls
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[McxLastSnapshot.__table__])
    ls._last_saved.clear()
    with Session(engine) as db:
        assert ls.save_snapshot(db, "99", "GOLD", {"ltp": "100"}, date(2026, 11, 24), now_monotonic=1000.0) is True
        assert ls.save_snapshot(db, "99", "GOLD", {"ltp": "101"}, date(2026, 11, 24), now_monotonic=1010.0) is False   # inside the throttle window
        assert ls.save_snapshot(db, "99", "GOLD", {"ltp": "102"}, date(2026, 11, 24), now_monotonic=1031.0) is True
        assert ls.save_snapshot(db, "99", "GOLD", {"ltp": "103"}, date(2026, 11, 24), force=True, now_monotonic=1032.0) is True   # session end
        assert ls.load_snapshots(db, ["99", "7"])["99"].payload["ltp"] == "103" and "7" not in ls.load_snapshots(db, ["99", "7"])
