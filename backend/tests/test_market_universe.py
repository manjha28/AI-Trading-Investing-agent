"""GET /market-data/universe: the Indian market-watch skeleton. Test rows below are TEST DATA only."""
from collections.abc import Iterator
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from uuid import uuid4
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api import market_data
from app.db import Base, get_db
from app.main import app
from app.models import (
    CorporateAction, Instrument, InstrumentProviderIdentifier, MarketBar, User, ValuationProviderInput,
)

NOW = datetime.now(timezone.utc)


@pytest.fixture
def engine():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    yield eng
    Base.metadata.drop_all(eng)
    eng.dispose()


@pytest.fixture
def client(engine) -> Iterator[TestClient]:
    def override_get_db():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def login(client: TestClient, engine, email: str = "watch@example.com") -> None:
    creds = {"email": email, "password": "correct-horse-battery"}
    assert client.post("/api/v1/auth/register", json=creds).status_code == 202
    assert client.post("/api/v1/auth/login", json=creds).status_code == 200
    with Session(engine) as db:
        db.scalar(select(User).where(User.email == email)).email_verified = True
        db.commit()


def stock(symbol: str, *, venue="NSE", asset="equity", country="IN", synthetic=False) -> Instrument:
    return Instrument(
        symbol=symbol, display_name=f"{symbol} Ltd", asset_class=asset, venue=venue, currency="INR", country=country,
        timezone="Asia/Kolkata", is_synthetic=synthetic, relink_required=False, created_at=NOW,
    )


def map_dhan(db: Session, instrument: Instrument, verified: bool = True) -> None:
    db.add(InstrumentProviderIdentifier(
        instrument_id=instrument.id, provider="dhan", provider_instrument_id=f"{abs(hash(instrument.symbol)) % 90000}|NSE_EQ",
        provider_symbol=instrument.symbol, is_verified=verified, last_seen_at=NOW,
    ))


def day_bar(db: Session, instrument: Instrument, session, close: str, volume: str = "1000") -> None:
    db.add(MarketBar(
        instrument_id=instrument.id, interval="1d", timestamp=datetime(session.year, session.month, session.day, tzinfo=timezone.utc),
        open=Decimal(close), high=Decimal(close), low=Decimal(close), close=Decimal(close), volume=Decimal(volume),
        source="dhan", retrieved_at=NOW, is_synthetic=False,
    ))


def sessions():
    """The last completed session and the one before it, from the app's own NSE calendar."""
    probe = stock("PROBE")
    midnight = lambda d: datetime.combine(d, time.min, tzinfo=ZoneInfo("Asia/Kolkata")).astimezone(timezone.utc)  # noqa: E731
    last = market_data._expected_market_session_date(probe, NOW)
    return last, market_data._expected_market_session_date(probe, midnight(last))


def test_universe_lists_only_dhan_mapped_nse_instruments_with_server_computed_changes(client, engine):
    login(client, engine)
    last, prior = sessions()
    older = prior - timedelta(days=3)
    with Session(engine) as db:
        rows = {name: stock(name) for name in ("UPCO", "DIVCO", "NOPREV", "NOBARS")}
        excluded = {
            "UNVERIFIED": stock("UNVERIFIED"), "BSEONLY": stock("BSEONLY", venue="BSE"),
            "CRYPTO": stock("CRYPTO", asset="crypto"), "SYNTH": stock("SYNTH", synthetic=True),
            "US": stock("US", country="US"),
        }
        db.add_all([*rows.values(), *excluded.values()])
        db.flush()
        for name, inst in {**rows, **excluded}.items():
            map_dhan(db, inst, verified=name != "UNVERIFIED")
        day_bar(db, rows["UPCO"], last, "110", "5000"); day_bar(db, rows["UPCO"], prior, "100")
        day_bar(db, rows["DIVCO"], last, "99"); day_bar(db, rows["DIVCO"], prior, "100")
        day_bar(db, rows["NOPREV"], last, "50"); day_bar(db, rows["NOPREV"], older, "48")  # prior session missing
        user_id = db.scalar(select(User.id))
        db.add(CorporateAction(user_id=user_id, instrument_id=rows["DIVCO"].id, action_type="dividend", ex_date=last,
                               amount=Decimal("1"), source="manual", created_at=NOW))
        db.add(ValuationProviderInput(instrument_id=rows["UPCO"].id, provider="dhan", metric="sector", text_value="Energy", fetched_at=NOW))
        db.commit()

    body = client.get("/api/v1/market-data/universe").json()
    by_symbol = {row["symbol"]: row for row in body["instruments"]}
    assert set(by_symbol) == {"UPCO", "DIVCO", "NOPREV", "NOBARS"} and body["count"] == 4
    assert [row["symbol"] for row in body["instruments"]] == sorted(by_symbol)  # stable alphabetical order

    up = by_symbol["UPCO"]
    assert (up["last_close"], up["previous_close"], up["change"], up["change_pct"]) == ("110.00000000", "100.00000000", "10.00000000", "10.00")
    assert up["sector"] == "Energy" and up["volume"] == "5000.00000000" and up["last_close_date"] == last.isoformat()
    assert by_symbol["DIVCO"]["change_pct"] == "0.00"  # 1.00 dividend on the ex-date adjusts the prior close: flat, not -1%
    assert by_symbol["NOPREV"]["change"] is None and prior.isoformat() in by_symbol["NOPREV"]["reason"]
    assert by_symbol["NOBARS"]["last_close"] is None and by_symbol["NOBARS"]["reason"] == "no stored daily close"


def test_universe_requires_a_verified_session(client, engine):
    assert client.get("/api/v1/market-data/universe").status_code == 401
    creds = {"email": "unverified@example.com", "password": "correct-horse-battery"}
    client.post("/api/v1/auth/register", json=creds)
    client.post("/api/v1/auth/login", json=creds)
    assert client.get("/api/v1/market-data/universe").status_code == 403


def test_universe_stream_route_exists_and_is_session_gated(client):
    """The live stream reuses the watchlist implementation; without a session it is closed with a refusal code."""
    from starlette.websockets import WebSocketDisconnect

    with client.websocket_connect("/api/v1/market-data/universe/stream", headers={"origin": "http://localhost:5173"}) as socket:
        first = socket.receive_json()
        assert first["type"] == "status" and first["status"] == "unavailable"  # told why, no ticks
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_json()
    assert closed.value.code in (4401, 4403, 4404)  # session or origin refused; nothing streamed


def _walk_back(sessions_from, count: int):
    """TEST HELPER: successive earlier NSE sessions using the app's own calendar."""
    probe = stock("PROBE")
    out, current = [], sessions_from
    for _ in range(count):
        current = market_data._expected_market_session_date(
            probe, datetime.combine(current, time.min, tzinfo=ZoneInfo("Asia/Kolkata")).astimezone(timezone.utc))
        out.append(current)
    return out


def test_universe_never_presents_an_old_close_as_current(client, engine):
    """The ONGC case: daily bars stop days ago. The numbers are true for their own date, so they are kept,
    but the row is flagged stale with the reason and the date, instead of looking current."""
    login(client, engine)
    probe = stock("PROBE")
    completed = market_data._last_completed_session(probe, NOW)
    prev1, prev2, prev3 = _walk_back(completed, 3)
    with Session(engine) as db:
        names = {n: stock(n) for n in ("FRESH", "OLDCLOSE", "NONE")}
        db.add_all(names.values())
        db.flush()
        for inst in names.values():
            map_dhan(db, inst)
        day_bar(db, names["FRESH"], completed, "110"); day_bar(db, names["FRESH"], prev1, "100")
        day_bar(db, names["OLDCLOSE"], prev2, "234.80"); day_bar(db, names["OLDCLOSE"], prev3, "233.50")
        db.commit()
    body = client.get("/api/v1/market-data/universe").json()
    rows = {r["symbol"]: r for r in body["instruments"]}
    assert body["expected_session"] == completed.isoformat()
    assert rows["FRESH"]["stale"] is False and rows["FRESH"]["reason"] is None and rows["FRESH"]["change_pct"] == "10.00"
    old = rows["OLDCLOSE"]
    assert old["stale"] is True and old["last_close"] == "234.80000000" and old["last_close_date"] == prev2.isoformat()
    assert old["sessions_behind"] == 2 and rows["FRESH"]["sessions_behind"] == 0  # 2+ = genuinely stale
    assert old["reason"].startswith(f"last stored close is {prev2.isoformat()}; {completed.isoformat()} is the latest completed session")
    assert rows["NONE"]["stale"] is True and rows["NONE"]["last_close"] is None


def test_refresh_fetches_real_closes_only_for_stale_symbols_in_bounded_batches(engine):
    from app import market_data_scheduler

    market_data_scheduler._universe_last_attempt.clear()
    probe = stock("PROBE")
    completed = market_data._last_completed_session(probe, NOW)
    prev1 = _walk_back(completed, 1)[0]
    stale_day = _walk_back(completed, 3)[2]
    calls: list[dict] = []

    from types import SimpleNamespace

    def fake_sync(**kwargs):
        calls.append(kwargs)
        if kwargs["instrument_id"] == bad_id:
            raise RuntimeError("provider timeout")
        if kwargs["instrument_id"] == soft_id:  # the real endpoint reports provider failure in its response
            return SimpleNamespace(sync_status="unavailable")
        return SimpleNamespace(sync_status="success")

    with Session(engine) as db:
        db.add(User(email="refresh@example.com", password_hash="x", email_verified=True, created_at=NOW))
        db.flush()
        user = db.scalar(select(User))
        fresh, old_a, old_b, none_c = (stock(n) for n in ("FRESH", "OLDA", "OLDB", "NOBARS"))
        db.add_all([fresh, old_a, old_b, none_c])
        db.flush()
        bad_id, soft_id = old_b.id, old_a.id
        for inst in (fresh, old_a, old_b, none_c):
            map_dhan(db, inst)
        day_bar(db, fresh, completed, "10"); day_bar(db, fresh, prev1, "9")
        day_bar(db, old_a, stale_day, "50")
        day_bar(db, old_b, stale_day, "60")
        db.commit()

        report = market_data_scheduler.refresh_stale_universe_closes(db, user, object(), limit=10, now=NOW, sync=fake_sync)
        assert report == {"stale": 3, "attempted": 3, "failed": 2}  # a raised error and a reported "unavailable" both count; the rest still run
        assert [c["instrument_id"] for c in calls] == [none_c.id, old_a.id, old_b.id]  # deterministic: by symbol
        assert fresh.id not in {c["instrument_id"] for c in calls}  # current symbols are never re-fetched
        by_id = {c["instrument_id"]: c for c in calls}
        assert all(c["interval"] == "1d" and c["_csrf"] is None for c in calls)
        assert by_id[old_a.id]["start"].date() == stale_day - timedelta(days=4)  # re-asks from just before its last bar
        assert by_id[none_c.id]["start"] == NOW - timedelta(days=12)  # no bars at all: a recent window

        # symbols just asked are not re-asked for a few hours (e.g. today's candle is not published yet)
        calls.clear()
        again = market_data_scheduler.refresh_stale_universe_closes(db, user, object(), limit=10, now=NOW + timedelta(minutes=20), sync=fake_sync)
        assert again["stale"] == 3 and again["attempted"] == 0 and calls == []
        market_data_scheduler._universe_last_attempt.clear()
        limited = market_data_scheduler.refresh_stale_universe_closes(db, user, object(), limit=1, now=NOW, sync=fake_sync)
        assert limited["stale"] == 3 and limited["attempted"] == 1 and len(calls) == 1  # bounded per cycle


def _watch(db: Session, user_id, instrument: Instrument, name: str = "Default") -> None:
    from app.models import Watchlist, WatchlistInstrument
    wl = db.scalar(select(Watchlist).where(Watchlist.user_id == user_id, Watchlist.name == name))
    if wl is None:
        wl = Watchlist(user_id=user_id, name=name, created_at=NOW)
        db.add(wl)
        db.flush()
    db.add(WatchlistInstrument(user_id=user_id, watchlist_id=wl.id, instrument_id=instrument.id, created_at=NOW))


def test_universe_always_includes_the_callers_watchlist_even_outside_the_dhan_feed(client, engine):
    login(client, engine)
    last, prior = sessions()
    with Session(engine) as db:
        user_id = db.scalar(select(User.id))
        feed = stock("FEEDCO")
        bse_only = stock("BSEWATCH", venue="BSE")           # not served by the Dhan feed, but the user watches it
        other_users = stock("OTHERSWATCH", venue="BSE")      # on someone else's watchlist only
        synthetic = stock("SYNTHWATCH", synthetic=True)      # never shown
        db.add_all([feed, bse_only, other_users, synthetic])
        db.flush()
        map_dhan(db, feed)
        day_bar(db, feed, last, "10")
        # a real stored bar from another source: change is only computed between bars of ONE source
        for session_date, close, source in ((last, "200", "yfinance"), (prior, "190", "yfinance")):
            db.add(MarketBar(instrument_id=bse_only.id, interval="1d", source=source, retrieved_at=NOW, is_synthetic=False,
                             timestamp=datetime(session_date.year, session_date.month, session_date.day, tzinfo=timezone.utc),
                             open=Decimal(close), high=Decimal(close), low=Decimal(close), close=Decimal(close), volume=Decimal("1")))
        _watch(db, user_id, bse_only)
        _watch(db, user_id, synthetic)
        db.commit()
        other_users_id = other_users.id

    body = client.get("/api/v1/market-data/universe").json()
    by_symbol = {row["symbol"]: row for row in body["instruments"]}
    assert set(by_symbol) == {"FEEDCO", "BSEWATCH"}, by_symbol.keys()
    assert by_symbol["FEEDCO"]["live_feed"] is True
    watched = by_symbol["BSEWATCH"]
    assert watched["live_feed"] is False
    assert watched["last_close"] == "200.00000000" or Decimal(watched["last_close"]) == Decimal("200")
    assert Decimal(watched["change"]) == Decimal("10") and watched["change_pct"] == "5.26"
    assert str(other_users_id) not in {row["instrument_id"] for row in body["instruments"]}


def test_a_watchlisted_symbol_with_no_stored_bars_is_listed_with_an_explicit_unknown(client, engine):
    login(client, engine)
    with Session(engine) as db:
        user_id = db.scalar(select(User.id))
        lone = stock("LONEWATCH", venue="BSE")
        db.add(lone)
        db.flush()
        _watch(db, user_id, lone)
        db.commit()
    row = client.get("/api/v1/market-data/universe").json()["instruments"][0]
    assert row["symbol"] == "LONEWATCH" and row["live_feed"] is False
    assert row["last_close"] is None and row["change_pct"] is None and row["reason"] == "no stored daily close"


def test_master_list_additions_are_separate_from_the_watchlist_and_owner_scoped(client, engine):
    login(client, engine)
    with Session(engine) as db:
        outside = stock("OUTSIDER", venue="BSE")
        outside2 = stock("OUTSIDER2", venue="BSE")
        db.add_all([outside, outside2])
        db.commit()
        outside_id, outside2_id = str(outside.id), str(outside2.id)
    csrf = client.cookies.get("csrf_token") or ""
    headers = {"X-CSRF-Token": csrf}
    assert client.get("/api/v1/market-data/universe").json()["count"] == 0

    assert client.post(f"/api/v1/market-data/universe/extras/{outside_id}", headers=headers).status_code == 204
    assert client.post(f"/api/v1/market-data/universe/extras/{outside_id}", headers=headers).status_code == 204  # idempotent
    body = client.get("/api/v1/market-data/universe").json()
    row = body["instruments"][0]
    assert body["count"] == 1 and row["symbol"] == "OUTSIDER" and row["master_extra"] is True and row["live_feed"] is False
    # in the master list does NOT mean on the watchlist
    assert all(item["instrument_id"] != outside_id for item in client.get("/api/v1/market-data/watchlist").json())

    # another user's additions never appear here
    other = TestClient(app)
    login(other, engine, "other-watch@example.com")
    assert other.get("/api/v1/market-data/universe").json()["count"] == 0

    assert client.delete(f"/api/v1/market-data/universe/extras/{outside_id}", headers=headers).status_code == 204
    assert client.get("/api/v1/market-data/universe").json()["count"] == 0
    nonexistent_id = str(uuid4())            # was outside2_id[:-1] + "0", which IS the real id about 1 run in 16 (when it already ends in 0) and made this assertion flaky
    assert nonexistent_id != str(outside2_id)
    assert client.post(f"/api/v1/market-data/universe/extras/{nonexistent_id}", headers=headers).status_code in (404, 422)


def test_master_list_additions_require_csrf_and_reject_synthetic(client, engine):
    login(client, engine)
    with Session(engine) as db:
        synth = stock("SYNTHX", venue="BSE", synthetic=True)
        db.add(synth)
        db.commit()
        synth_id = str(synth.id)
    assert client.post(f"/api/v1/market-data/universe/extras/{synth_id}").status_code in (400, 403)  # no CSRF header
    csrf = client.cookies.get("csrf_token") or ""
    assert client.post(f"/api/v1/market-data/universe/extras/{synth_id}", headers={"X-CSRF-Token": csrf}).status_code == 422


def test_universe_rows_carry_the_last_bars_day_range(client, engine):
    login(client, engine)
    last, _ = sessions()
    with Session(engine) as db:
        inst = stock("RANGECO")
        db.add(inst)
        db.flush()
        map_dhan(db, inst)
        db.add(MarketBar(instrument_id=inst.id, interval="1d", source="dhan", retrieved_at=NOW, is_synthetic=False,
                         timestamp=datetime(last.year, last.month, last.day, tzinfo=timezone.utc),
                         open=Decimal("100"), high=Decimal("108.5"), low=Decimal("97.25"), close=Decimal("105"), volume=Decimal("1")))
        db.commit()
    row = client.get("/api/v1/market-data/universe").json()["instruments"][0]
    assert Decimal(row["day_high"]) == Decimal("108.5") and Decimal(row["day_low"]) == Decimal("97.25")
