from datetime import datetime
from zoneinfo import ZoneInfo

from app.history_tier_backfill import covered, quiet_hours, windows

IST = ZoneInfo("Asia/Kolkata")


def at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=IST)


def test_downloads_only_while_the_market_is_closed():
    assert not quiet_hours(at(27, 8, 50)) and not quiet_hours(at(27, 12)) and not quiet_hours(at(27, 16, 0))
    assert quiet_hours(at(27, 8, 30)) and quiet_hours(at(27, 17)) and quiet_hours(at(28, 12))      # Saturday


def test_windows_cover_the_whole_span_without_overlap():
    spans = windows(at(27, 18), 183)
    assert spans[0][1] == at(27, 0) and (spans[0][1] - spans[-1][0]).days == 183
    assert all(older[1] == newer[0] for newer, older in zip(spans, spans[1:]))


def test_a_window_counts_as_covered_only_when_most_weekdays_are_stored():
    begin, end = at(2, 0), at(16, 0)           # ten weekdays
    assert covered(9, begin, end) and not covered(8, begin, end) and not covered(0, begin, end)
