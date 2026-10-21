from dataclasses import dataclass, field
from datetime import timezone
from decimal import Decimal, ROUND_HALF_EVEN
from typing import Iterable

from app.models import PortfolioTransaction

CENT = Decimal("0.01")
ZERO = Decimal("0")


class LedgerError(ValueError):
    pass


def money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_EVEN)


def _utc(value):
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@dataclass
class Position:
    quantity: Decimal = ZERO
    cost_basis: Decimal = ZERO


@dataclass
class PortfolioLedger:
    cash_balance: Decimal = ZERO
    realized_pnl: Decimal = ZERO
    positions: dict[str, Position] = field(default_factory=dict)


def calculate_ledger(transactions: Iterable[PortfolioTransaction]) -> PortfolioLedger:
    ledger = PortfolioLedger()
    ordered = sorted(
        transactions,
        key=lambda transaction: (
            _utc(transaction.occurred_at),
            _utc(transaction.created_at),
            str(transaction.id),
        ),
    )

    for transaction in ordered:
        amount = money(transaction.amount)
        fee = money(transaction.fee)

        if transaction.transaction_type == "deposit":
            ledger.cash_balance += amount - fee
            ledger.realized_pnl -= fee
        elif transaction.transaction_type == "withdrawal":
            ledger.cash_balance -= amount + fee
            ledger.realized_pnl -= fee
        elif transaction.transaction_type == "dividend":
            ledger.cash_balance += amount - fee
            ledger.realized_pnl += amount - fee
        elif transaction.transaction_type in {"buy", "sell"}:
            symbol = transaction.symbol
            quantity = transaction.quantity
            if symbol is None or quantity is None or transaction.unit_price is None:
                raise LedgerError("Equity trades require a symbol, quantity, and unit price")
            gross = money(quantity * transaction.unit_price)
            position = ledger.positions.setdefault(symbol, Position())

            if transaction.transaction_type == "buy":
                total_cost = gross + fee
                ledger.cash_balance -= total_cost
                position.quantity += quantity
                position.cost_basis += total_cost
            else:
                if quantity > position.quantity:
                    raise LedgerError(f"Sale exceeds the available {symbol} position")
                proceeds = gross - fee
                allocated_cost = (
                    position.cost_basis
                    if quantity == position.quantity
                    else money(position.cost_basis * quantity / position.quantity)
                )
                ledger.cash_balance += proceeds
                ledger.realized_pnl += proceeds - allocated_cost
                position.quantity -= quantity
                position.cost_basis -= allocated_cost
                if position.quantity == ZERO:
                    ledger.positions.pop(symbol)

        if ledger.cash_balance < ZERO:
            raise LedgerError("Transaction would make portfolio cash negative")

        ledger.cash_balance = money(ledger.cash_balance)
        ledger.realized_pnl = money(ledger.realized_pnl)

    return ledger