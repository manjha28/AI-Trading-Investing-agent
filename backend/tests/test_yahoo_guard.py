from app import yahoo_guard


def test_guard_trips_after_consecutive_failures_and_recovers_on_reset():
    yahoo_guard.reset()
    assert yahoo_guard.allowed()
    for _ in range(yahoo_guard.FAILURES_TO_TRIP - 1):
        yahoo_guard.record(False)
    assert yahoo_guard.allowed()
    yahoo_guard.record(True)                      # a success resets the run
    for _ in range(yahoo_guard.FAILURES_TO_TRIP):
        yahoo_guard.record(False)
    assert not yahoo_guard.allowed() and yahoo_guard.seconds_left() > 0
    yahoo_guard.reset()
    yahoo_guard.trip()
    assert not yahoo_guard.allowed()
    yahoo_guard.reset()
