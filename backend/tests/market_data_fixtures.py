from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.market_data import (
    HistoricalBar,
    InstrumentCandidate,
    MarketDataProviderError,
    ProviderHealth,
)


class RecordedMarketDataProvider:
    name = "recorded"
    is_mock = False
    _catalog = (
        InstrumentCandidate(
            provider="recorded",
            provider_instrument_id="FIXTURE-US-ALPHA-EQ-001",
            provider_symbol="DEMO_US:ALPHA",
            symbol="ALPHA",
            display_name="Alpha Fixture Corporation",
            asset_class="equity",
            venue="DEMO_US",
            currency="USD",
            country="US",
            timezone="UTC",
            is_synthetic=False,
        ),
        InstrumentCandidate(
            provider="recorded",
            provider_instrument_id="FIXTURE-IN-ALPHA-EQ-001",
            provider_symbol="DEMO_IN:ALPHA",
            symbol="ALPHA",
            display_name="Alpha India Fixture Limited",
            asset_class="equity",
            venue="DEMO_IN",
            currency="INR",
            country="IN",
            timezone="UTC",
            is_synthetic=False,
        ),
    )

    def search_instruments(self, query: str, limit: int) -> list[InstrumentCandidate]:
        normalized = query.casefold().strip()
        if not normalized:
            raise MarketDataProviderError("search_query_required")
        return [
            item for item in self._catalog
            if normalized in item.symbol.casefold()
            or normalized in item.display_name.casefold()
            or normalized in item.provider_symbol.casefold()
        ][:limit]

    def get_instrument(self, provider_instrument_id: str) -> InstrumentCandidate:
        for item in self._catalog:
            if item.provider_instrument_id == provider_instrument_id:
                return item
        raise MarketDataProviderError("instrument_not_found")

    def get_historical_bars(
        self,
        instrument: InstrumentCandidate,
        interval: str,
        start: datetime,
        end: datetime,
        limit: int,
    ) -> list[HistoricalBar]:
        if interval not in {"1d", "1h", "15m", "5m", "1m"}:
            raise MarketDataProviderError("unsupported_interval")
        if start.utcoffset() is None or end.utcoffset() is None or start >= end:
            raise MarketDataProviderError("invalid_time_range")
        step = {
            "1d": timedelta(days=1),
            "1h": timedelta(hours=1),
            "15m": timedelta(minutes=15),
            "5m": timedelta(minutes=5),
            "1m": timedelta(minutes=1),
        }[interval]
        cursor = start.astimezone(timezone.utc)
        end_utc = end.astimezone(timezone.utc)
        offset = sum(instrument.provider_instrument_id.encode("utf-8")) % 37
        bars = []
        index = 0
        while cursor < end_utc and len(bars) < limit:
            opening = Decimal("50") + Decimal(offset) + Decimal(index) / Decimal("10")
            closing = opening + Decimal("0.25")
            bars.append(HistoricalBar(
                timestamp=cursor,
                open=opening,
                high=closing + Decimal("0.75"),
                low=opening - Decimal("0.5"),
                close=closing,
                volume=Decimal(1000 + (index * 17) % 5000),
            ))
            cursor += step
            index += 1
        return bars

    def get_latest_quote(self, instrument: InstrumentCandidate) -> HistoricalBar:
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        return HistoricalBar(
            timestamp=now,
            open=Decimal("50"),
            high=Decimal("52"),
            low=Decimal("49"),
            close=Decimal("51"),
            volume=Decimal("1200"),
        )

    def health(self) -> ProviderHealth:
        return ProviderHealth(
            name=self.name,
            healthy=True,
            is_mock=False,
            capabilities=("instrument_search", "instrument_lookup", "historical_ohlcv", "latest_quote"),
            status="recorded_test_fixture",
        )
