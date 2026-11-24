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
