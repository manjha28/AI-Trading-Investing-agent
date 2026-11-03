"""Shared closed-trade P&L and analytics definitions used by the Journal and backtests."""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_EVEN
from typing import Iterable, Protocol


CENT = Decimal("0.01")


class TradePnlInput(Protocol):
    side: str
    entry_price: Decimal
    exit_price: Decimal | None
    quantity: Decimal
    multiplier: Decimal
    fees: Decimal


def net_pnl(trade: TradePnlInput) -> Decimal | None:
    if trade.exit_price is None:
        return None
    direction = Decimal("1") if trade.side == "long" else Decimal("-1")
    gross = direction * (trade.exit_price - trade.entry_price) * trade.quantity * trade.multiplier
    return (gross - trade.fees).quantize(CENT, rounding=ROUND_HALF_EVEN)


def summarize_net_pnls(values: Iterable[Decimal]) -> dict[str, int | Decimal | None]:
    """Journal definitions: closed net results in exit-time order, starting equity and peak at zero."""
    results = list(values)
    wins = [value for value in results if value > 0]
    losses = [value for value in results if value < 0]
    net_total = sum(results, Decimal("0")).quantize(CENT, rounding=ROUND_HALF_EVEN)
    gross_wins = sum(wins, Decimal("0"))
    gross_losses = -sum(losses, Decimal("0"))
    equity = Decimal("0")
    peak = Decimal("0")
    max_drawdown = Decimal("0")
    for result in results:
        equity += result
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
    return {
        "closed_trades": len(results),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": (
            (Decimal(len(wins)) / Decimal(len(results)) * 100).quantize(CENT, rounding=ROUND_HALF_EVEN)
            if results else None
        ),
        "net_pnl": net_total,
        "expectancy": (
            (net_total / Decimal(len(results))).quantize(CENT, rounding=ROUND_HALF_EVEN)
            if results else None
        ),
        "profit_factor": (
            (gross_wins / gross_losses).quantize(CENT, rounding=ROUND_HALF_EVEN)
            if gross_losses else None
        ),
        "max_drawdown": max_drawdown.quantize(CENT, rounding=ROUND_HALF_EVEN),
    }
