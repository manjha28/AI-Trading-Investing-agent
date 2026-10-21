from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, field_validator, model_validator


class MarketDataProviderError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class InstrumentCandidate(BaseModel):
    provider: str
    provider_instrument_id: str = Field(min_length=1, max_length=128)
    provider_symbol: str = Field(min_length=1, max_length=96)
    symbol: str = Field(min_length=1, max_length=64)
    display_name: str = Field(min_length=1, max_length=160)
    asset_class: str
    venue: str = Field(min_length=1, max_length=32)
    currency: str = Field(pattern=r"^[A-Z]{3,4}$")
    country: str = Field(pattern=r"^[A-Z]{2}$")
    timezone: str = Field(min_length=1, max_length=64)
    multiplier: Decimal | None = Field(default=None, gt=0, max_digits=18, decimal_places=8)
    expiry: date | None = None
    strike: Decimal | None = Field(default=None, gt=0, max_digits=24, decimal_places=8)
    option_type: str | None = None
    is_synthetic: bool
    is_verified: bool = False

    @field_validator("symbol", "display_name", "provider_symbol")
    @classmethod
    def trim_required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("value cannot be blank")
        return value

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as error:
            raise ValueError("timezone must be a valid IANA timezone") from error
        return value

    @model_validator(mode="after")
    def validate_asset_metadata(self):
        if self.asset_class not in {"equity", "etf", "index", "future", "option", "crypto"}:
            raise ValueError("unsupported asset class")
        if self.asset_class == "option":
            if self.expiry is None or self.strike is None or self.option_type not in {"call", "put"}:
                raise ValueError("options require expiry, strike, and call/put type")
            if self.multiplier is None:
                raise ValueError("options require a contract multiplier")
        elif self.asset_class == "future":
            if self.expiry is None or self.multiplier is None:
                raise ValueError("futures require expiry and contract multiplier")
            if self.strike is not None or self.option_type is not None:
                raise ValueError("futures cannot have option metadata")
        elif any(value is not None for value in (self.expiry, self.strike, self.option_type, self.multiplier)):
            raise ValueError("contract metadata is not valid for this asset class")
        return self


class HistoricalBar(BaseModel):
    timestamp: datetime
    open: Decimal = Field(gt=0, max_digits=24, decimal_places=8)
    high: Decimal = Field(gt=0, max_digits=24, decimal_places=8)
    low: Decimal = Field(gt=0, max_digits=24, decimal_places=8)
    close: Decimal = Field(gt=0, max_digits=24, decimal_places=8)
    volume: Decimal = Field(ge=0, max_digits=30, decimal_places=8)

    @field_validator("timestamp")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("bar timestamp must include a timezone")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def validate_ohlc(self):
        if self.low > self.high or not self.low <= self.open <= self.high:
            raise ValueError("bar low/high do not contain the open")
        if not self.low <= self.close <= self.high:
            raise ValueError("bar low/high do not contain the close")
        return self


@dataclass(frozen=True)
class ProviderHealth:
    name: str
    healthy: bool
    is_mock: bool
    capabilities: tuple[str, ...]
    status: str


class MarketDataProvider(Protocol):
    name: str
    is_mock: bool

    def search_instruments(self, query: str, limit: int) -> list[InstrumentCandidate]: ...

    def get_instrument(self, provider_instrument_id: str) -> InstrumentCandidate: ...

    def get_historical_bars(
        self,
        instrument: InstrumentCandidate,
        interval: str,
        start: datetime,
        end: datetime,
        limit: int,
    ) -> list[HistoricalBar]: ...

    def get_latest_quote(self, instrument: InstrumentCandidate) -> HistoricalBar: ...

    def health(self) -> ProviderHealth: ...


class MockMarketDataProvider:
    name = "mock"
    is_mock = True
    _catalog = (
        InstrumentCandidate(
            provider="mock",
            provider_instrument_id="DEMO-US-ALPHA-EQ-001",
            provider_symbol="DEMO_US:ALPHA",
            symbol="ALPHA",
            display_name="Alpha Demo Corporation",
            asset_class="equity",
            venue="DEMO_US",
            currency="USD",
            country="US",
            timezone="America/New_York",
            is_synthetic=True,
        ),
        InstrumentCandidate(
            provider="mock",
            provider_instrument_id="DEMO-IN-ALPHA-EQ-001",
            provider_symbol="DEMO_IN:ALPHA",
            symbol="ALPHA",
            display_name="Alpha India Demo Limited",
            asset_class="equity",
            venue="DEMO_IN",
            currency="INR",
            country="IN",
            timezone="Asia/Kolkata",
            is_synthetic=True,
        ),
    )

    def search_instruments(self, query: str, limit: int) -> list[InstrumentCandidate]:
        normalized = query.casefold().strip()
        if not normalized:
            raise MarketDataProviderError("search_query_required")
        matches = [
            item
            for item in self._catalog
            if normalized in item.symbol.casefold()
            or normalized in item.display_name.casefold()
            or normalized in item.provider_symbol.casefold()
        ]
        return matches[:limit]

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
        if interval not in {"1d", "1h", "15m", "1m", "5m"}:
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
        bars: list[HistoricalBar] = []
        index = 0
        while cursor < end_utc and len(bars) < limit:
            baseline = Decimal("50") + Decimal(offset) + Decimal(index) / Decimal("10")
            opening = baseline
            closing = baseline + Decimal("0.25")
            bars.append(
                HistoricalBar(
                    timestamp=cursor,
                    open=opening,
                    high=closing + Decimal("0.75"),
                    low=opening - Decimal("0.5"),
                    close=closing,
                    volume=Decimal(1000 + (index * 17) % 5000),
                )
            )
            cursor += step
            index += 1
        return bars

    def get_latest_quote(self, instrument: InstrumentCandidate) -> HistoricalBar:
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        bar = self.get_historical_bars(
            instrument, "1h", now - timedelta(hours=1), now, 1
        )[0]
        return bar.model_copy(update={"timestamp": datetime.now(timezone.utc)})

    def health(self) -> ProviderHealth:
        return ProviderHealth(
            name=self.name,
            healthy=True,
            is_mock=True,
            capabilities=("instrument_search", "instrument_lookup", "historical_ohlcv", "latest_quote"),
            status="deterministic_synthetic_fixture",
        )
