"""One real Dhan fill stored twice (trade book + trade history) made every trade count double and left the day's P&L unknown."""
import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from app.api.trades import _collapse_history_and_book_copies, _match_dhan_fills
from app.models import DhanTradeMirror

FIXTURE = Path(__file__).parent / "fixtures" / "dhan_fills_2026_11_23_anonymised.json"   # real day, ids hashed; test data only


def _rows() -> list[DhanTradeMirror]:
    out = []
    for r in json.loads(FIXTURE.read_text()):
        out.append(DhanTradeMirror(
            id=uuid4(), user_id=uuid4(), source=r["source"], trade_id=r["trade_id"], provider_trade_id=r["provider_trade_id"], order_id=r["order_id"],
            exchange_order_id=r["exchange_order_id"], symbol=r["symbol"], security_id=r["security_id"], exchange_segment=r["exchange_segment"], side=r["side"],
            quantity=Decimal(r["quantity"]), price=Decimal(r["price"]), charges=None if r["charges"] is None else Decimal(r["charges"]), currency="INR",
            trade_at=datetime.fromisoformat(r["trade_at"])))
    return out


def test_real_day_is_collapsed_to_one_row_per_fill_and_every_trade_has_known_charges():
    rows = _rows()
    assert len(rows) == 340                                   # as stored: every fill twice
    kept = _collapse_history_and_book_copies(rows)
    assert len(kept) == 154                                   # 153 history rows (partial fills summed) + 1 book-only fill that history does not list yet
    assert sum(1 for r in kept if r.charges is None) == 1
    from app.api.trades import _classify_segment
    from app.services.dhan_charge_estimate import estimate_missing_charges
    kept.sort(key=lambda r: (r.trade_at, r.trade_id))
    kept = estimate_missing_charges(kept, lambda r: _classify_segment(r.exchange_segment, r.symbol) == "option")   # the book-only fill gets a labelled estimate
    assert all(r.charges is not None for r in kept) and sum(1 for r in kept if getattr(r, "charges_estimated", False)) == 1
    result = _match_dhan_fills(kept)
    assert len(result["matches"]) > 0
    assert all(m["net_pnl"] is not None for m in result["matches"])          # so the day's P&L is a number, not blank
    print("matches", len(result["matches"]), "open", len(result["open_fill_ids"]))
    by_id = {str(r.id): r for r in kept}
    gross = sum(((by_id[m["exit_fill_id"]].price - by_id[m["entry_fill_id"]].price) * m["quantity"] * (1 if m["side"] == "long" else -1) for m in result["matches"]), Decimal(0))
    print("gross", gross, "net", sum(m["net_pnl"] for m in result["matches"]), "fees", sum(m["fees"] for m in result["matches"]))


def test_genuine_separate_fills_are_never_merged():
    a, b = _rows()[:2]
    twin = DhanTradeMirror(**{c.key: getattr(a, c.key) for c in a.__table__.columns if c.key != "id"}, id=uuid4())
    twin.trade_id = "other"
    # two history copies with identical order/price/quantity are two separate fills
    assert a.provider_trade_id == "0" or b.provider_trade_id == "0"
    history = a if a.provider_trade_id == "0" else b
    second = DhanTradeMirror(**{c.key: getattr(history, c.key) for c in history.__table__.columns if c.key != "id"}, id=uuid4())
    second.trade_id = "second"
    assert len(_collapse_history_and_book_copies([history, second])) == 2


def test_trade_book_fill_already_in_history_is_not_added_again_by_the_sync():
    from app.api.brokers import _merge_trade_book
    hist = [{"exchangeTradeId": "0", "orderId": "9", "exchangeOrderId": "8", "securityId": "64476", "transactionType": "BUY", "tradedQuantity": 65, "tradedPrice": 61.2}]

    class Reader:
        def get_trade_book(self):
            return [{"exchangeTradeId": "3290893", "orderId": "9", "exchangeOrderId": "8", "securityId": "64476", "transactionType": "BUY", "tradedQuantity": 65, "tradedPrice": 61.2},
                    {"exchangeTradeId": "3290894", "orderId": "10", "exchangeOrderId": "11", "securityId": "64476", "transactionType": "SELL", "tradedQuantity": 65, "tradedPrice": 66.0}]
    rows, _ = _merge_trade_book(Reader(), hist)
    assert [r["orderId"] for r in rows] == ["9", "10"]
