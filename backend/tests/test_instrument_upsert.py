"""Instrument search/upsert: resolve by canonical identity, never duplicate, race-safe.

PostgreSQL's uq_instruments_identity is NULLS NOT DISTINCT. SQLite treats NULLs as distinct, so the test
database gets an equivalent COALESCE expression index to reproduce the production collision."""
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api import market_data
from app.api.market_data import _upsert_provider_instrument
from app.db import Base
from app.market_data import InstrumentCandidate, MarketDataProviderError
from app.models import Instrument, InstrumentProviderIdentifier

NOW = datetime(2026, 11, 5, 5, 0, tzinfo=timezone.utc)


@pytest.fixture
def engine():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(text("DROP INDEX IF EXISTS uq_instruments_identity"))
        connection.execute(text(
            "CREATE UNIQUE INDEX uq_instruments_identity ON instruments "
            "(venue, symbol, asset_class, COALESCE(expiry, ''), COALESCE(option_type, ''), COALESCE(strike, ''))"
        ))
    yield engine
    Base.metadata.drop_all(engine)
    engine.dispose()


def candidate(symbol="BEL", provider="dhan", provider_id="383|NSE_EQ", verified=False, name="Bharat Electronics",
              venue="NSE", **over):
    data = dict(provider=provider, provider_instrument_id=provider_id, provider_symbol=symbol, symbol=symbol,
                display_name=name, asset_class="equity", venue=venue, currency="INR", country="IN",
                timezone="Asia/Kolkata", is_synthetic=False, is_verified=verified)
    data.update(over)
    return InstrumentCandidate(**data)


def count(session):
    return session.scalar(select(func.count()).select_from(Instrument))


def test_existing_bel_found_by_canonical_identity_when_the_provider_id_differs(engine):
    with Session(engine) as db:
        original = _upsert_provider_instrument(db, candidate(provider="angel_one", provider_id="BEL-EQ"), NOW)
        db.commit()
        original_id = original.id
        # The production failure: same (NSE, BEL, equity, NULL, NULL, NULL), different provider/id.
        again = _upsert_provider_instrument(db, candidate(provider="dhan", provider_id="383|NSE_EQ"), NOW)
        db.commit()
        assert again.id == original_id and count(db) == 1
        assert {m.provider for m in again.provider_identifiers} == {"angel_one", "dhan"}


def test_same_provider_new_id_for_an_existing_instrument_does_not_duplicate_or_remap(engine):
    with Session(engine) as db:
        first = _upsert_provider_instrument(db, candidate(provider_id="383|NSE_EQ", verified=True), NOW)
        db.commit()
        again = _upsert_provider_instrument(db, candidate(provider_id="999|NSE_EQ", verified=False), NOW)
        db.commit()
        assert again.id == first.id and count(db) == 1
        mappings = db.scalars(select(InstrumentProviderIdentifier)).all()
        assert [(m.provider_instrument_id, m.is_verified) for m in mappings] == [("383|NSE_EQ", True)]


def test_repeated_search_is_idempotent_and_never_unverifies_a_verified_mapping(engine):
    with Session(engine) as db:
        first = _upsert_provider_instrument(db, candidate(verified=True), NOW)
        db.commit()
        for _ in range(3):
            again = _upsert_provider_instrument(db, candidate(verified=False, name="BEL (renamed by provider)"), NOW)
            db.commit()
            assert again.id == first.id
        assert count(db) == 1
        mapping = db.scalar(select(InstrumentProviderIdentifier))
        assert mapping.is_verified is True and mapping.provider_symbol == "BEL"
        assert db.scalar(select(func.count()).select_from(InstrumentProviderIdentifier)) == 1
        assert first.display_name == "Bharat Electronics"  # a populated name is not rewritten by another listing


def test_blank_display_name_is_filled_but_identity_columns_are_never_rewritten(engine):
    with Session(engine) as db:
        first = _upsert_provider_instrument(db, candidate(provider="angel_one", provider_id="x"), NOW)
        first.display_name = " "
        db.commit()
        updated = _upsert_provider_instrument(db, candidate(provider="dhan", currency="INR", timezone="Asia/Kolkata"), NOW)
        assert updated.display_name == "Bharat Electronics"
        assert (updated.venue, updated.symbol, updated.asset_class) == ("NSE", "BEL", "equity")


def test_two_candidates_with_one_identity_in_one_search_create_one_instrument(engine):
    with Session(engine) as db:
        a = _upsert_provider_instrument(db, candidate(provider="angel_one", provider_id="BEL-EQ"), NOW)
        b = _upsert_provider_instrument(db, candidate(provider="dhan"), NOW)
        db.commit()
        assert a.id == b.id and count(db) == 1


def test_distinct_instruments_stay_distinct(engine):
    with Session(engine) as db:
        bel_nse = _upsert_provider_instrument(db, candidate(), NOW)
        bel_bse = _upsert_provider_instrument(db, candidate(provider_id="500049", venue="BSE"), NOW)
        bhel = _upsert_provider_instrument(db, candidate(symbol="BHEL", provider_id="438|NSE_EQ", name="BHEL"), NOW)
        fut = _upsert_provider_instrument(db, candidate(provider_id="f1", asset_class="future", multiplier=Decimal("100"), expiry=datetime(2026, 12, 31).date()), NOW)
        fut2 = _upsert_provider_instrument(db, candidate(provider_id="f2", asset_class="future", multiplier=Decimal("100"), expiry=datetime(2027, 1, 28).date()), NOW)
        opt = _upsert_provider_instrument(db, candidate(provider_id="o1", asset_class="option", expiry=datetime(2026, 12, 31).date(),
                                                         strike=Decimal("300"), multiplier=Decimal("100"), option_type="call"), NOW)
        opt_put = _upsert_provider_instrument(db, candidate(provider_id="o2", asset_class="option", expiry=datetime(2026, 12, 31).date(),
                                                             strike=Decimal("300"), multiplier=Decimal("100"), option_type="put"), NOW)
        db.commit()
        assert len({i.id for i in (bel_nse, bel_bse, bhel, fut, fut2, opt, opt_put)}) == 7 and count(db) == 7


def test_losing_a_concurrent_insert_race_adopts_the_winner_and_keeps_the_transaction_usable(engine, monkeypatch):
    """Simulate the race: the identity lookup misses (the other request has not committed yet), then the insert
    hits the unique index. The loser must roll back only its savepoint and adopt the winner's row."""
    with Session(engine) as winner_session:
        winner = _upsert_provider_instrument(winner_session, candidate(provider="angel_one", provider_id="BEL-EQ", verified=True), NOW)
        winner_session.commit()
        winner_id = winner.id

    real_find = market_data._find_instrument_by_identity
    calls = {"n": 0}

    def stale_first_lookup(db, cand):
        calls["n"] += 1
        return None if calls["n"] == 1 else real_find(db, cand)

    monkeypatch.setattr(market_data, "_find_instrument_by_identity", stale_first_lookup)
    with Session(engine) as loser:
        marker = Instrument(symbol="OTHER", display_name="Other", asset_class="equity", venue="NSE", currency="INR",
                            country="IN", timezone="Asia/Kolkata", created_at=NOW)
        loser.add(marker)  # earlier work in the same transaction must survive the conflict
        loser.flush()
        result = _upsert_provider_instrument(loser, candidate(provider="dhan"), NOW)
        loser.commit()
        assert result.id == winner_id and calls["n"] >= 2
        assert count(loser) == 2  # BEL once + OTHER
        providers = {m.provider: m.is_verified for m in result.provider_identifiers}
        assert providers == {"angel_one": True, "dhan": False}


def test_an_unresolvable_conflict_raises_a_domain_error_not_a_raw_integrity_error(engine, monkeypatch):
    with Session(engine) as db:
        _upsert_provider_instrument(db, candidate(provider="angel_one", provider_id="BEL-EQ"), NOW)
        db.commit()
        monkeypatch.setattr(market_data, "_find_instrument_by_identity", lambda *_a: None)
        with pytest.raises(MarketDataProviderError) as error:
            _upsert_provider_instrument(db, candidate(provider="dhan"), NOW)
        assert error.value.code == "instrument_identity_conflict"
        db.rollback()
        assert count(db) == 1


def test_synthetic_candidates_are_still_rejected(engine):
    with Session(engine) as db:
        with pytest.raises(MarketDataProviderError):
            _upsert_provider_instrument(db, candidate(is_synthetic=True), NOW)
