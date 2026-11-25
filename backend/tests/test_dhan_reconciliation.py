"""The day reconciliation: Dhan realised (gross) = journal gross, and journal net = gross - fees and taxes; contracts missing from the master are listed. Rows are TEST DATA."""
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dhan_reconciliation import build
from app.models import BrokerMirror, DhanTradeMirror, User
from app.services import dhan_daily_realised
from tests.test_crypto_delta import engine, login, client  # noqa: F401

D = Decimal
T1 = datetime(2026, 11, 23, 5, 0, tzinfo=timezone.utc)
T2 = datetime(2026, 11, 23, 5, 5, tzinfo=timezone.utc)
DAY = T1.astimezone(__import__("zoneinfo").ZoneInfo("Asia/Kolkata")).date()


def fill(user_id, n, side, qty, price, t, sec="64473", symbol="NIFTY-Nov2026-25500-CE", charges=None):
    return DhanTradeMirror(user_id=user_id, source="dhan", trade_id=f"t{n}", order_id=f"o{n}", symbol=symbol, security_id=sec, exchange_segment="NSE_FNO", side=side, quantity=qty, price=D(price), charges=charges,
                           currency="INR", trade_at=t, synced_at=t)


def test_dhan_realised_is_gross_and_the_gap_to_the_journal_net_is_exactly_the_fees(client, engine):  # noqa: F811
    login(client, engine, "recon@example.com")
    with Session(engine) as db:
        uid = db.scalar(select(User.id))
        db.add_all([fill(uid, 1, "BUY", 65, "20.00", T1), fill(uid, 2, "SELL", 65, "30.00", T2)])        # gross +650, charges unknown -> estimated
        db.add(BrokerMirror(user_id=uid, broker="dhan", kind="position", symbol="NIFTY-Nov2026-25500-CE", exchange="NSE_FNO", product="INTRADAY", security_id="64473", quantity=D(0), average_price=D(0),
                            position_type="CLOSED", realized_profit=D("650.00"), unrealized_profit=D(0), synced_at=T2))
        db.flush()
        dhan_daily_realised.record(db, uid, list(db.scalars(select(BrokerMirror).where(BrokerMirror.user_id == uid))), T2)   # what the sync stores that day
        db.commit()
        body = build(db, uid, DAY)
    assert body["dhan"]["realised"] == "650.00" and body["journal"]["gross"] == "650.00"
    est = D(body["journal"]["fees_and_taxes_estimated"])
    assert est > 0 and body["journal"]["fees_and_taxes_real"] == "0.00" and D(body["journal"]["net"]) == D("650.00") - est
    assert body["unexplained"] == "0.00" and body["reconciles_within_1_rupee"] is True and body["journal"]["trades_with_estimated_charges"] == 1


def test_a_real_gap_is_reported_as_unexplained_and_open_lot_differences_are_listed(client, engine):  # noqa: F811
    login(client, engine, "recon2@example.com")
    with Session(engine) as db:
        uid = db.scalar(select(User.id))
        db.add_all([fill(uid, 3, "BUY", 65, "20.00", T1, charges=D("10")), fill(uid, 4, "SELL", 65, "30.00", T2, charges=D("10")), fill(uid, 5, "BUY", 65, "40.00", T2, sec="999", symbol="NIFTY-Nov2026-25400-PE", charges=D("5"))])
        db.add(BrokerMirror(user_id=uid, broker="dhan", kind="position", symbol="NIFTY-Nov2026-25500-CE", exchange="NSE_FNO", product="INTRADAY", security_id="64473", quantity=D(0), average_price=D(0), position_type="CLOSED",
                            realized_profit=D("700.00"), unrealized_profit=D(0), synced_at=T2))
        db.add(BrokerMirror(user_id=uid, broker="dhan", kind="position", symbol="NIFTY-Nov2026-25400-PE", exchange="NSE_FNO", product="INTRADAY", security_id="999", quantity=D(0), average_price=D(0), position_type="CLOSED",
                            realized_profit=D(0), unrealized_profit=D(0), synced_at=T2))
        db.flush()
        dhan_daily_realised.record(db, uid, list(db.scalars(select(BrokerMirror).where(BrokerMirror.user_id == uid))), T2)
        db.commit()
        body = build(db, uid, DAY)
    assert body["unexplained"] == "50.00" and body["reconciles_within_1_rupee"] is False and body["journal"]["fees_and_taxes_real"] == "20.00"
    assert [d for d in body["open_lot_differences"] if d["security_id"] == "999"] == [{"security_id": "999", "journal_net_quantity": "65", "dhan_net_quantity": "0"}]


def test_contracts_missing_from_the_master_are_listed_with_why(client, engine):  # noqa: F811
    login(client, engine, "recon3@example.com")
    with Session(engine) as db:
        uid = db.scalar(select(User.id))
        db.add(fill(uid, 6, "BUY", 25, "50.00", T1, sec="51343", symbol="NIFTY 27 OCT 22750 PUT", charges=D("5")))
        db.commit()
        body = build(db, uid, DAY)
    assert body["contracts_without_master"][0]["symbol"] == "NIFTY 27 OCT 22750 PUT" and "expired contracts drop out" in body["contracts_without_master"][0]["reason"] and "27 Oct" in body["contracts_without_master"][0]["reason"]


def test_dhan_reconciliation_route_answers_without_portfolio_id(client, engine):  # noqa: F811
    login(client, engine, "recon2@example.com")
    r = client.get("/api/v1/brokers/dhan/reconciliation")           # the generic /{slug}/reconciliation used to shadow this and 422
    assert r.status_code == 200 and "journal" in r.json() and "dhan" in r.json()


def test_a_day_dhan_figure_was_never_captured_for_is_unknown_never_a_later_days_positions(client, engine):  # noqa: F811
    from datetime import timedelta
    login(client, engine, "recon5@example.com")
    with Session(engine) as db:
        uid = db.scalar(select(User.id))
        db.add_all([fill(uid, 7, "BUY", 65, "20.00", T1, charges=D("10")), fill(uid, 8, "SELL", 65, "30.00", T2, charges=D("10"))])
        later = T2 + timedelta(days=2)                                 # a positions snapshot from ANOTHER day must not be read as this day's Dhan figure
        db.add(BrokerMirror(user_id=uid, broker="dhan", kind="position", symbol="X", exchange="NSE_FNO", product="INTRADAY", security_id="1", quantity=D(0), average_price=D(0),
                            position_type="CLOSED", realized_profit=D("123.00"), unrealized_profit=D(0), synced_at=later))
        db.commit()
        body = build(db, uid, DAY)
    assert body["dhan"]["realised"] is None and body["unexplained"] is None and "current day only" in body["dhan"]["note"]


def test_an_empty_positions_fetch_records_zero_and_a_missing_figure_records_unknown(engine):  # noqa: F811
    with Session(engine) as db:
        user = User(email="dd@example.com", password_hash="x", email_verified=True, created_at=T1)
        db.add(user); db.flush()
        assert dhan_daily_realised.record(db, user.id, [], T2).realised == 0
        partial = [BrokerMirror(user_id=user.id, broker="dhan", kind="position", symbol="A", quantity=D(0), realized_profit=D("5"), synced_at=T2),
                   BrokerMirror(user_id=user.id, broker="dhan", kind="position", symbol="B", quantity=D(0), realized_profit=None, synced_at=T2)]
        row = dhan_daily_realised.record(db, user.id, partial, T2)
        assert row.realised is None and row.positions_count == 2
