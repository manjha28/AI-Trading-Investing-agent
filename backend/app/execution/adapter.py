"""The seam between a signal and a fill.

``FillAdapter`` is the one interface. Today only ``BacktestFillAdapter`` exists: it replays a
COMPLETED historical bar and returns a hypothetical fill. There is deliberately no live adapter
and no method here that can reach a broker's order endpoints; a future live adapter is a separate
owner-approved milestone that would implement the same interface.
"""
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from app.execution.intent import OrderIntent


@dataclass(frozen=True)
class Bar:
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    completed: bool


@dataclass(frozen=True)
class SimulatedFill:
    filled: bool
    price: Decimal | None
    quantity: Decimal
    fee: Decimal | None
    reason: str | None
    hypothetical: bool = True  # always: never a position, an order, or money at risk


class FillAdapter(Protocol):
    def fill(self, intent: OrderIntent, bar: Bar, *, contract_value: Decimal) -> SimulatedFill: ...


class BacktestFillAdapter:
    def __init__(self, fee_rate: Decimal = Decimal("0.0005"), slippage_bps: Decimal = Decimal("0")) -> None:
        self.fee_rate = fee_rate
        self.slippage = slippage_bps / Decimal("10000")

    def fill(self, intent: OrderIntent, bar: Bar, *, contract_value: Decimal) -> SimulatedFill:
        def miss(reason: str) -> SimulatedFill:
            return SimulatedFill(False, None, Decimal("0"), None, reason)

        if not bar.completed:
            return miss("bar_not_completed")  # no look-ahead: only completed bars are replayed
        buy = intent.side == "buy"
        price: Decimal | None = None
        if intent.order_type == "market":
            price = bar.open * (Decimal("1") + self.slippage if buy else Decimal("1") - self.slippage)
        elif intent.order_type == "limit":
            limit = intent.limit_price
            assert limit is not None
            if buy and bar.low <= limit:
                price = min(bar.open, limit)
            elif not buy and bar.high >= limit:
                price = max(bar.open, limit)
        elif intent.order_type == "stop_market":
            stop = intent.stop_price
            assert stop is not None
            if buy and bar.high >= stop:
                price = max(bar.open, stop) * (Decimal("1") + self.slippage)
            elif not buy and bar.low <= stop:
                price = min(bar.open, stop) * (Decimal("1") - self.slippage)
        else:
            return miss("stop_limit_not_modelled")
        if price is None:
            return miss("price_not_reached")
        return SimulatedFill(True, price, intent.quantity, intent.quantity * contract_value * price * self.fee_rate, None)
