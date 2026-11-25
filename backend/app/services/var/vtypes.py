"""Shared value types. Prices/money are Decimal; statistical series (ATR, slope, ratios) are float."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
BAR = timedelta(minutes=5)


def D(x: object) -> Decimal:
    """Decimal via string; Decimal(float) is forbidden."""
    if isinstance(x, Decimal):
        return x
    if isinstance(x, float):
        return Decimal(repr(x))
    return Decimal(str(x))


class DeltaQuality(str, Enum):
    EXCHANGE_CONFIRMED = "exchange_confirmed"
    QUOTE_CLASSIFIED = "quote_classified"
    TICK_ESTIMATED = "tick_estimated"
    UNCLASSIFIED = "unclassified"
    UNAVAILABLE = "unavailable"


class DataLevel(str, Enum):
    A_TRADE = "A_trade_level"
    B_TICK_ESTIMATED = "B_tick_estimated"
    C_CANDLE = "C_candle_approximate"


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class Cond(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    PENDING = "pending"
    UNAVAILABLE = "unavailable"
    INVALID_DATA = "invalid_data_quality"


@dataclass(frozen=True)
class Trade:
    ts: datetime                 # exchange timestamp (tz-aware)
    price: Decimal
    qty: Decimal
    ingested_at: datetime | None = None
    trade_id: str | None = None  # provider sequence id for dedupe; never dedupe on (ts, price, qty) alone
    source: str = "unknown"
    aggressor: Side | None = None  # only when the provider genuinely supplies it


@dataclass(frozen=True)
class Quote:
    ts: datetime
    bid: Decimal
    ask: Decimal
    bid_qty: Decimal | None = None
    ask_qty: Decimal | None = None


@dataclass(frozen=True)
class BarFlow:
    """Per-bar order flow. ``None`` on a Bar means order flow is unavailable (never zero)."""
    buy: Decimal
    sell: Decimal
    unclassified: Decimal
    quality: DeltaQuality

    @property
    def classified(self) -> Decimal:
        return self.buy + self.sell

    @property
    def total(self) -> Decimal:
        return self.buy + self.sell + self.unclassified

    @property
    def delta(self) -> Decimal:
        return self.buy - self.sell

    @property
    def coverage_pct(self) -> float:
        t = self.total
        return float(self.classified / t * 100) if t > 0 else 0.0

    def delta_ratio(self) -> float | None:
        c = self.classified
        return float((self.buy - self.sell) / c) if c > 0 else None


@dataclass(frozen=True)
class Bar:
    ts: datetime                 # bar OPEN time, tz-aware; the bar is observable only at ts + 5m
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    flow: BarFlow | None = None
    vol_at: dict[int, Decimal] | None = None   # tick index -> traded volume, when trade-exact data exists
    spread_ticks: float | None = None
    level: DataLevel = DataLevel.C_CANDLE

    @property
    def end(self) -> datetime:
        return self.ts + BAR


@dataclass(frozen=True)
class Contract:
    instrument_id: str
    symbol: str
    underlying: str
    kind: str                    # "index" | "stock"
    expiry: datetime | None
    lot_size: int
    tick_size: Decimal
    segment: str = "futures"             # "futures" | "equity_cash" (cash has lot size 1)

    @property
    def instrument_type(self) -> str:
        if self.segment == "equity_cash":
            return "cash"
        return "index_future" if self.kind == "index" else "stock_future"

    def __post_init__(self) -> None:
        if self.lot_size <= 0 or self.tick_size <= 0:
            raise ValueError("lot_size and tick_size must come from the instrument master and be positive")


@dataclass
class CondResult:
    name: str
    state: Cond
    detail: str = ""
    value: object | None = None

    def ok(self) -> bool:
        return self.state is Cond.PASS


@dataclass
class Signal:
    strategy: str
    setup: str                   # ACCEPTANCE_BREAKOUT | FAILED_AUCTION_REVERSAL
    instrument: str
    direction: str               # LONG | SHORT
    trigger_bar_end: datetime
    regime: str
    reference_level: Decimal
    reference_source: str
    entry: Decimal
    stop: Decimal
    targets: list[Decimal]
    risk_reward: float
    delta_quality: str
    data_level: str
    conditions: list[CondResult] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    derivation: dict[str, object] = field(default_factory=dict)
    expires_at: datetime | None = None
    signal_quality: str = "VALID"

    @property
    def failed(self) -> list[CondResult]:
        return [c for c in self.conditions if not c.ok()]


@dataclass
class Rejection:
    setup: str
    instrument: str
    direction: str
    bar_end: datetime
    reasons: list[str]
    conditions: list[CondResult] = field(default_factory=list)
