"""Dated lot-size lookup fails closed. TEST FIXTURE rows only."""
from datetime import date, datetime, timezone
from decimal import Decimal as D
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models.derivatives import DerivativeContract, FuturesCandle1m
from app.services.lot_sizes import LotSizeUnavailable, lot_size_on

NOW = datetime(2026, 11, 5, tzinfo=timezone.utc)


def contract(symbol, expiry, lot):
    return DerivativeContract(id=uuid4(), exchange="NSE", segment="D", master_segment="D", instrument_type="FUTIDX", security_id=symbol, underlying_symbol="NIFTY", trading_symbol=symbol,
                              expiry=expiry, lot_size=D(lot), tick_size=D("10"), status="active", valid_from=NOW, first_seen_at=NOW, last_seen_at=NOW, spec_hash="x", source="t", master_version="t", ingested_at=NOW)


@pytest.fixture
def db():
    e = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(e)
    with Session(e) as s:
        yield s


def test_lot_comes_from_the_contract_with_candles_on_that_date_and_fails_closed_otherwise(db):
    nov, dec = contract("NIFTY-NOV", date(2026, 11, 23), 65), contract("NIFTY-DEC", date(2026, 12, 29), 65)
    db.add_all([nov, dec])
    db.flush()
    db.add(FuturesCandle1m(contract_id=nov.id, ts=datetime(2026, 9, 2, 4, 0, tzinfo=timezone.utc), open=D(1), high=D(1), low=D(1), close=D(1), volume=1, open_interest=0, source="t", retrieved_at=NOW))
    db.commit()
    got = lot_size_on(db, "NIFTY", date(2026, 9, 2))
    assert got.lot_size == 65 and got.contract == "NIFTY-NOV" and got.tick_size_rupees == D("0.1")
    with pytest.raises(LotSizeUnavailable):
        lot_size_on(db, "NIFTY", date(2026, 1, 15))          # an older date must NOT silently get today's lot (NIFTY was 75 before Jan 2026)
    with pytest.raises(LotSizeUnavailable):
        lot_size_on(db, "NIFTY", date(2026, 9, 3))           # no candles that day -> no evidence the contract was live
    with pytest.raises(LotSizeUnavailable):
        lot_size_on(db, "BANKNIFTY", date(2026, 9, 2))
