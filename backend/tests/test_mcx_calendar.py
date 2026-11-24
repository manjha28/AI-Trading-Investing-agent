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
