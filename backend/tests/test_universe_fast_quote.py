"""The Market watch fast path builds a live quote with no database access; change is Decimal math."""
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import time_machine

from app.api.market_data import PriorCloseValue, _fast_universe_quote
from app.broker_adapters import HistoricalBar
from app.models import Instrument

OPEN_NOW = datetime(2026, 11, 23, 5, 0, 0, tzinfo=timezone.utc)      # Monday 10:30 IST, NSE open (test clock only)
CLOSED_NOW = datetime(2026, 11, 22, 5, 0, 0, tzinfo=timezone.utc)    # Sunday


def _instrument() -> Instrument:
    return Instrument(
        id=uuid4(), symbol="TESTCO", display_name="Test Co", asset_class="equity", venue="NSE", currency="INR",
        country="IN", timezone="Asia/Kolkata", is_synthetic=False, relink_required=False,
        created_at=OPEN_NOW, provider_identifiers=[],
    )


def _bar(at: datetime) -> HistoricalBar:  # TEST DATA: a fixture bar, never used at runtime
    return HistoricalBar(
        timestamp=at, open=Decimal("100"), high=Decimal("103"), low=Decimal("99"), close=Decimal("102.50"),
        volume=Decimal("1000"),
    )


@time_machine.travel(OPEN_NOW, tick=False)
def test_change_uses_feed_previous_close_in_decimal() -> None:
    entry = (_instrument(), None, OPEN_NOW.date())
    quote = _fast_universe_quote(entry, _bar(OPEN_NOW), Decimal("100"))
    assert quote is not None
    assert quote.absolute_change == Decimal("2.50")
    assert quote.percent_change == Decimal("2.5000")
    assert "previous-close packet" in (quote.change_basis or "")


@time_machine.travel(OPEN_NOW, tick=False)
def test_change_is_unknown_without_any_previous_close() -> None:
    quote = _fast_universe_quote((_instrument(), None, OPEN_NOW.date()), _bar(OPEN_NOW), None)
    assert quote is not None
    assert quote.absolute_change is None and quote.percent_change is None


@time_machine.travel(OPEN_NOW, tick=False)
def test_falls_back_to_stored_prior_close() -> None:
    prior = PriorCloseValue(close=Decimal("101"), session_date=datetime(2026, 11, 20).date(), source="dhan")
    quote = _fast_universe_quote((_instrument(), prior, OPEN_NOW.date()), _bar(OPEN_NOW), None)
    assert quote is not None and quote.absolute_change == Decimal("1.50")


@time_machine.travel(CLOSED_NOW, tick=False)
def test_closed_market_yields_no_live_quote() -> None:
    assert _fast_universe_quote((_instrument(), None, None), _bar(CLOSED_NOW), Decimal("100")) is None
