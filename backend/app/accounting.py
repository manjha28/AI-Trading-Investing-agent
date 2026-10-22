from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from decimal import Decimal, ROUND_HALF_EVEN
from typing import Iterable
from uuid import UUID

from app.models import PortfolioTransaction

CENT = Decimal("0.01")
ZERO = Decimal("0")
QUANTITY_QUANTUM = Decimal("0.00000001")


class LedgerError(ValueError):
    pass


def money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_EVEN)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@dataclass
class Position:
    quantity: Decimal = ZERO
    cost_basis: Decimal = ZERO
    acquisition_lots: list["AcquisitionLot"] = field(default_factory=list)

    @property
    def holding_period_start(self) -> datetime | None:
        return min((lot.acquired_at for lot in self.acquisition_lots), default=None)


@dataclass
class AcquisitionLot:
    quantity: Decimal
    acquired_at: datetime


@dataclass(frozen=True)
class CorporateActionAdjustment:
    id: UUID
    symbol: str
    action_type: str
    ex_date: date
    ratio: Decimal | None
    amount: Decimal | None


@dataclass
class PortfolioLedger:
    cash_balance: Decimal = ZERO
    realized_pnl: Decimal = ZERO
    positions: dict[str, Position] = field(default_factory=dict)


def calculate_ledger(
    transactions: Iterable[PortfolioTransaction],
    corporate_actions: Iterable[CorporateActionAdjustment] = (),
) -> PortfolioLedger:
    ledger = PortfolioLedger()
    events = [
        (
            _utc(transaction.occurred_at),
            2,
            _utc(transaction.created_at),
            str(transaction.id),
            "transaction",
            transaction,
        )
        for transaction in transactions
    ]
    for action in corporate_actions:
        event_time = datetime.combine(action.ex_date, time.min, tzinfo=timezone.utc)
        action_order = 0 if action.action_type in {"split", "bonus"} else 1
        events.append(
            (event_time, action_order, event_time, str(action.id), "corporate_action", action)
        )

    for _occurred_at, _priority, _created_at, _identifier, event_type, event in sorted(
        events, key=lambda item: item[:4]
    ):
        if event_type == "corporate_action":
            action = event
            position = ledger.positions.get(action.symbol)
            if position is None or position.quantity == ZERO:
                continue
            if action.action_type == "split":
                if action.ratio is None or action.ratio <= ZERO:
                    raise LedgerError("Split actions require a positive share ratio")
                for lot in position.acquisition_lots:
                    lot.quantity = (lot.quantity * action.ratio).quantize(QUANTITY_QUANTUM)
                position.quantity = sum(
                    (lot.quantity for lot in position.acquisition_lots), ZERO
                )
                if position.quantity <= ZERO:
                    raise LedgerError("Split ratio leaves no supported fractional shares")
            elif action.action_type == "bonus":
                if action.ratio is None or action.ratio <= ZERO:
                    raise LedgerError("Bonus actions require a positive share ratio")
                bonus_quantity = (position.quantity * action.ratio).quantize(QUANTITY_QUANTUM)
                if bonus_quantity <= ZERO:
                    raise LedgerError("Bonus ratio leaves no supported fractional shares")
                position.quantity += bonus_quantity
                position.acquisition_lots.append(
                    AcquisitionLot(
                        quantity=bonus_quantity,
                        acquired_at=datetime.combine(action.ex_date, time.min, tzinfo=timezone.utc),
                    )
                )
            elif action.action_type == "dividend":
                if action.amount is None or action.amount <= ZERO:
                    raise LedgerError("Dividend actions require a positive per-share amount")
                dividend = money(position.quantity * action.amount)
                ledger.cash_balance += dividend
                ledger.realized_pnl += dividend
            else:
                raise LedgerError("Unsupported corporate action type")
            ledger.cash_balance = money(ledger.cash_balance)
            ledger.realized_pnl = money(ledger.realized_pnl)
            continue

        transaction = event
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
                if position.quantity == ZERO:
                    position.acquisition_lots.clear()
                position.acquisition_lots.append(
                    AcquisitionLot(quantity=quantity, acquired_at=_utc(transaction.occurred_at))
                )
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
                shares_to_remove = quantity
                while shares_to_remove > ZERO and position.acquisition_lots:
                    lot = position.acquisition_lots[0]
                    removed = min(lot.quantity, shares_to_remove)
                    lot.quantity -= removed
                    shares_to_remove -= removed
                    if lot.quantity == ZERO:
                        position.acquisition_lots.pop(0)
                if position.quantity == ZERO:
                    ledger.positions.pop(symbol)

        if ledger.cash_balance < ZERO:
            raise LedgerError("Transaction would make portfolio cash negative")

        ledger.cash_balance = money(ledger.cash_balance)
        ledger.realized_pnl = money(ledger.realized_pnl)

    return ledger