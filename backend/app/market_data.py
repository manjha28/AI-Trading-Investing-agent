from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, field_validator, model_validator


class MarketDataProviderError(Exception):
    def __init__(
        self,
        code: str,
        *,
        status_code: int | None = None,
        retry_after: str | None = None,
        response_body: str | None = None,
        request_url: str | None = None,
        request_body: str | None = None,
    ):
        super().__init__(code)
        self.code = code
        self.status_code = status_code
        self.retry_after = retry_after
        self.response_body = response_body
        self.request_url = request_url
        self.request_body = request_body


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
