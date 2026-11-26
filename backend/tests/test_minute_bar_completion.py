from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.minute_bar_completion import due

IST = ZoneInfo("Asia/Kolkata")


def at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=IST)


def test_runs_once_after_the_close_on_trading_days_only():
    assert not due(at(27, 15, 40), None)                 # Friday, session data not final yet
    assert due(at(27, 15, 45), None)
    assert due(at(27, 22, 0), date(2026, 11, 26))        # yesterday's run does not count for today
    assert not due(at(27, 18, 0), date(2026, 11, 27))    # already done today
    assert not due(at(28, 18, 0), None)                  # Saturday
