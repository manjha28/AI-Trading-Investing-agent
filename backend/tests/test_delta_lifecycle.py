"""Order outcome from Delta's own records. The records below are the real Delta replies for the two live orders placed on 24-25 Nov 2026 (ids shortened, fields trimmed)."""
from decimal import Decimal

from app.delta_execution import lifecycle

D = Decimal


def _order(id, created, side, kind, state, avg=None, cid=None, stop_type=None, stop=None, bracket=None, reduce=False, why=None):
    return {"id": id, "created_at": created, "side": side, "order_type": kind, "state": state, "average_fill_price": avg, "client_order_id": cid, "stop_order_type": stop_type,
            "stop_price": stop, "bracket_order": bracket, "reduce_only": reduce, "cancellation_reason": why, "product_id": 27}


def _fill(order_id, created, side, size, price, commission, realized=None, fill_type="normal"):
    return {"order_id": order_id, "created_at": created, "side": side, "size": size, "price": price, "commission": commission, "product_id": 27, "fill_type": fill_type,
            "meta_data": {"new_position": {"realized_pnl": realized}}}


ORDERS = [
    _order(1712685749, "2026-11-24T18:20:49.580Z", "sell", "limit_order", "closed", "59997", cid="CID1"),
    _order(1712685751, "2026-11-24T18:20:49.724Z", "buy", "market_order", "cancelled", None, stop_type="stop_loss_order", stop="60265", bracket=True, reduce=True, why="position_closed"),
    _order(1712685752, "2026-11-24T18:20:49.725Z", "buy", "market_order", "closed", "59340.5", stop_type="take_profit_order", stop="59341", bracket=True, reduce=True),
    _order(1713140076, "2026-11-25T02:00:16.532Z", "buy", "limit_order", "closed", "59225.5", cid="CID2"),
    _order(1713140082, "2026-11-25T02:00:16.674Z", "sell", "market_order", "closed", "59015.5", stop_type="stop_loss_order", stop="59050", bracket=True, reduce=True),
    _order(1713140083, "2026-11-25T02:00:16.674Z", "sell", "market_order", "cancelled", None, stop_type="take_profit_order", stop="59826", bracket=True, reduce=True, why="position_closed"),
]
FILLS = [
    _fill(1712685749, "2026-11-24T18:20:49Z", "sell", "1", "59997", "0.03539823"),
    _fill(1712685752, "2026-11-25T01:32:18Z", "buy", "1", "59340.5", "0.0350109", realized="0.65650000"),
    _fill(1713140076, "2026-11-25T02:00:16Z", "buy", "1", "59225.5", "0.03494305"),
    _fill(1713140082, "2026-11-25T02:08:06Z", "sell", "1", "59015.5", "0", realized="-0.21000000"),
]
TXNS = [{"created_at": "2026-11-25T00:09:51Z", "transaction_type": "funding", "amount": "0.00414048"}, {"created_at": "2026-11-08T08:09:51Z", "transaction_type": "funding", "amount": "-0.018"}]


def test_target_hit_matches_the_delta_app():
    got = lifecycle.classify("CID1", ORDERS, FILLS, TXNS, False)
    assert got["outcome"] == "target_hit" and got["exit_price"] == D("59340.5") and got["realized_pnl"] == D("0.6565")
    assert got["fees"] == D("0.07040913") and got["funding"] == D("0.00414048")
    assert got["net_pnl"] == D("0.59023135")                       # 0.6565 - 0.07040913 + 0.00414048


def test_stop_hit_matches_the_delta_app():
    got = lifecycle.classify("CID2", ORDERS, FILLS, TXNS, False)
    assert got["outcome"] == "stop_hit" and got["exit_price"] == D("59015.5") and got["realized_pnl"] == D("-0.21") and got["funding"] == 0
    assert got["net_pnl"] == D("-0.24494305")


def test_open_position_unfilled_cancelled_manual_and_liquidated():
    still = lifecycle.classify("CID2", [o for o in ORDERS if o["id"] in (1713140076, 1713140083, 1713140082) and o["id"] != 1713140082], FILLS[2:3], [], True)
    assert still["outcome"] == "still_open" and still["net_pnl"] is None
    assert lifecycle.classify("NOPE", ORDERS, FILLS, [], False)["outcome"] == "unknown"
    cancelled = [_order(5, "2026-11-25T03:00:00Z", "buy", "limit_order", "cancelled", None, cid="C5", why="cancelled_by_user")]
    assert lifecycle.classify("C5", cancelled, [], [], False)["outcome"] == "cancelled"
    entry = [ORDERS[3]]                                              # entry filled, no bracket leg filled, position gone, a later opposite fill exists
    manual = lifecycle.classify("CID2", entry, [FILLS[2], _fill(99, "2026-11-25T02:30:00Z", "sell", "1", "59100", "0.03", realized="-0.12")], [], False)
    assert manual["outcome"] == "closed_manually" and manual["exit_price"] == D("59100")
    liq = lifecycle.classify("CID2", entry, [FILLS[2], _fill(99, "2026-11-25T02:30:00Z", "sell", "1", "55300", "0", realized="-3.9", fill_type="liquidation")], [], False)
    assert liq["outcome"] == "liquidated"


def test_report_marks_data_stale_with_the_reason_when_delta_cannot_be_read(monkeypatch):
    from datetime import datetime, timezone
    from uuid import uuid4

    from app.delta_execution import report

    class Down:
        def _request(self, *a, **k):
            from app.delta_execution.client import BrokerReply
            return BrokerReply(False, 500, {}, "boom")

    monkeypatch.setitem(report._cache, "data", {"wallet": {"total": "3.87"}, "user_id": "u", "synced_at": "2026-11-25T03:00:00+00:00"})
    uid = uuid4()
    monkeypatch.setitem(report._cache, "at", -1e9)
    out = report.report(None, uid, lambda: Down(), now=datetime(2026, 11, 25, 4, tzinfo=timezone.utc))
    assert out["stale"] is True and out["error"] and out["wallet"]["total"] == "3.87"        # last good copy kept, flagged stale


def test_a_rejected_order_reads_rejected_not_unknown_and_open_legs_are_found():
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from app.delta_execution import report
    row = SimpleNamespace(id="x", created_at=datetime(2026, 11, 24, 2, 16, tzinfo=timezone.utc), symbol="BTCUSD", side="buy", contracts=1, leverage=25, status="rejected",
                          entry_price=None, stop_price=D("59000"), target_price=D("60000"), block_reason="Delta rejected the order (HTTP 400): insufficient_margin")
    view = report._order_view(row, None)
    assert view["outcome"] == "rejected" and view["outcome_label"].startswith("Rejected")
    # the 25 Nov 05:20 short: entry filled, its stop and target are still OPEN orders (Delta's history only lists finished ones)
    entry = _order(1713524209, "2026-11-25T05:20:52.575Z", "sell", "limit_order", "closed", "59211.5", cid="CID3")
    legs = [_order(1713524211, "2026-11-25T05:20:52.712Z", "buy", "market_order", "open", None, stop_type="stop_loss_order", stop="59253.5", bracket=True, reduce=True),
            _order(1713524212, "2026-11-25T05:20:52.713Z", "buy", "market_order", "open", None, stop_type="take_profit_order", stop="58715.5", bracket=True, reduce=True)]
    got = lifecycle.classify("CID3", [entry, *legs], [_fill(1713524209, "2026-11-25T05:20:52Z", "sell", "1", "59211.5", "0.035")], [], True)
    assert got["outcome"] == "still_open" and sorted(got["bracket_leg_ids"]) == [1713524211, 1713524212]
