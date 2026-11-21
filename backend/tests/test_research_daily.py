from datetime import datetime, timezone

from app.research_daily import due_today


def test_due_only_after_close_on_trading_days():  # test data: fixed instants, no market data
    assert not due_today(datetime(2026, 11, 20, 9, 0, tzinfo=timezone.utc))      # Fri 14:30 IST, before 16:30
    assert due_today(datetime(2026, 11, 20, 11, 30, tzinfo=timezone.utc))        # Fri 17:00 IST
    assert not due_today(datetime(2026, 11, 21, 11, 30, tzinfo=timezone.utc))    # Saturday
