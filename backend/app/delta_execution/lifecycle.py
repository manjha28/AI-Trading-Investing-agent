"""What happened to each live Delta order, taken from Delta's own records (order history, fills, wallet transactions, positions). Read only.

Delta attaches a bracket to the entry as two reduce-only orders created right after it (`bracket_order` true, `stop_order_type` stop_loss_order /
take_profit_order). Whichever fills is the exit; the other is cancelled with reason `position_closed`. P&L figures are Delta's: realised P&L from the closing
fill's position record, commission (GST included) from the fills, funding from the wallet transactions between entry and exit."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

BRACKET_WINDOW = timedelta(seconds=10)
OUTCOMES = ("target_hit", "stop_hit", "closed_manually", "liquidated", "still_open", "cancelled", "unknown")
LABELS = {"target_hit": "Target hit", "stop_hit": "Stop hit", "closed_manually": "Closed manually", "liquidated": "Liquidated", "still_open": "Still open",
          "cancelled": "Cancelled", "rejected": "Rejected by Delta (never filled)", "unknown": "Unknown"}


def _ts(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _dec(value: Any) -> Decimal | None:
    try:
        return None if value in (None, "") else Decimal(str(value))
    except ArithmeticError:
        return None


def classify(client_order_id: str, orders: list[dict[str, Any]], fills: list[dict[str, Any]], transactions: list[dict[str, Any]], position_open: bool | None) -> dict[str, Any]:
    """One live order's outcome and money from Delta's records. `position_open` is whether Delta shows an open position in that contract now."""
    entry = next((o for o in orders if o.get("client_order_id") == client_order_id), None)
    if entry is None:
        return {"outcome": "unknown", "reason": "the order was not found in Delta's order history"}
    entry_fills = [f for f in fills if str(f.get("order_id")) == str(entry["id"])]
    if not entry_fills:
        return {"outcome": "cancelled" if str(entry.get("state")) == "cancelled" else "unknown", "entry_order_id": entry["id"], "reason": entry.get("cancellation_reason") or "no fill"}
    entry_at = max(_ts(f["created_at"]) for f in entry_fills)
    size = sum(Decimal(str(f["size"])) for f in entry_fills)
    entry_price = sum(Decimal(str(f["price"])) * Decimal(str(f["size"])) for f in entry_fills) / size
    created = _ts(entry.get("created_at")) or entry_at
    legs = [o for o in orders if o.get("bracket_order") and o.get("reduce_only") and o.get("product_id") == entry.get("product_id")
            and (_ts(o.get("created_at")) or created) - created <= BRACKET_WINDOW and (_ts(o.get("created_at")) or created) >= created]
    hit = next((o for o in legs if str(o.get("state")) == "closed" and o.get("average_fill_price")), None)
    close_fills: list[dict[str, Any]] = []
    outcome, exit_order_id = "still_open", None
    if hit is not None:
        outcome = "target_hit" if hit.get("stop_order_type") == "take_profit_order" else "stop_hit"
        exit_order_id = hit["id"]
        close_fills = [f for f in fills if str(f.get("order_id")) == str(hit["id"])]
    elif position_open is False:
        # no bracket leg filled but the position is gone: closed by hand, or liquidated
        opposite = "sell" if entry.get("side") == "buy" else "buy"
        later = sorted((f for f in fills if f.get("product_id") == entry.get("product_id") and f.get("side") == opposite and (_ts(f["created_at"]) or entry_at) > entry_at), key=lambda f: f["created_at"])
        if later:
            close_fills = [later[0]]
            exit_order_id = later[0].get("order_id")
            outcome = "liquidated" if str(later[0].get("fill_type")) == "liquidation" else "closed_manually"
        else:
            outcome = "unknown"
    out: dict[str, Any] = {"outcome": outcome, "entry_order_id": entry["id"], "entry_price": entry_price, "entry_at": entry_at, "exit_order_id": exit_order_id,
                           "bracket_leg_ids": [o["id"] for o in legs]}
    if not close_fills:
        out.update(exit_price=None, exit_at=None, realized_pnl=None, fees=sum((_dec(f.get("commission")) or Decimal(0) for f in entry_fills), Decimal(0)), funding=None, net_pnl=None)
        return out
    csize = sum(Decimal(str(f["size"])) for f in close_fills)
    exit_price = sum(Decimal(str(f["price"])) * Decimal(str(f["size"])) for f in close_fills) / csize
    exit_at = max(_ts(f["created_at"]) for f in close_fills)
    realized = _dec(((close_fills[-1].get("meta_data") or {}).get("new_position") or {}).get("realized_pnl"))
    fees = sum((_dec(f.get("commission")) or Decimal(0) for f in entry_fills + close_fills), Decimal(0))
    funding = sum((_dec(t.get("amount")) or Decimal(0) for t in transactions if t.get("transaction_type") == "funding" and entry_at <= (_ts(t.get("created_at")) or entry_at) <= exit_at), Decimal(0))
    net = None if realized is None else realized - fees + funding
    out.update(exit_price=exit_price, exit_at=exit_at, realized_pnl=realized, fees=fees, funding=funding, net_pnl=net)
    return out
