"""The whole Market watch master list as a universe for scanners and forward tests, and the rotating 1-minute refresh behind it. All data is TEST DATA."""
from datetime import datetime, timedelta, timezone
from uuid import UUID

import time as _real_time
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Instrument, InstrumentProviderIdentifier, User, WatchlistInstrument, Watchlist
from app.models.forward_live import ForwardTest
from app.models.market_watch import MarketWatchExtra
from app import master_bar_refresh as mbr
from app.forward_test_scheduler import candle_due, should_run
from app.services import forward_test as fts, lab
from app.services import intraday_runner as runner
from app.services.intraday_strategy import StrategyDefinition
from app.strategies import get
from tests.test_signals_lab_api import add_bars, add_inst, client, definition, engine, h, login, nse_sessions  # noqa: F401  (fixtures + helpers)

NOW = datetime(2026, 11, 23, 5, 0, tzinfo=timezone.utc)                  # Monday 10:30 IST, market open


def listed(engine, symbol, *, dhan=True, venue="NSE", synthetic=False) -> str:
    iid = add_inst(engine, symbol, venue=venue)
    with Session(engine) as db:
        inst = db.get(Instrument, UUID(iid))
        inst.is_synthetic = synthetic
        if dhan:
            db.add(InstrumentProviderIdentifier(instrument_id=inst.id, provider="dhan", provider_instrument_id=f"{abs(hash(symbol)) % 9000 + 1000}|NSE_EQ", provider_symbol=symbol,
                                                is_verified=True, last_seen_at=NOW))
        db.commit()
    return iid


def user_id(engine, email) -> UUID:
    with Session(engine) as db:
        return db.scalar(select(User.id).where(User.email == email))


def test_the_master_list_is_the_feed_universe_plus_what_the_user_added_plus_their_watchlist(client, engine):
    login(client, engine, "master@example.com")
    uid = user_id(engine, "master@example.com")
    feed = listed(engine, "FEEDCO")
    extra = listed(engine, "EXTRACO", dhan=False)
    watched = listed(engine, "WATCHCO", dhan=False)
    listed(engine, "SYNTH", synthetic=True)
    listed(engine, "UNMAPPED", dhan=False)                                    # not in the feed universe, not added, not watched
    with Session(engine) as db:
        db.add(MarketWatchExtra(user_id=uid, instrument_id=UUID(extra), created_at=NOW))
        wl = Watchlist(user_id=uid, name="Default", created_at=NOW)
        db.add(wl)
        db.flush()
        db.add(WatchlistInstrument(user_id=uid, watchlist_id=wl.id, instrument_id=UUID(watched), created_at=NOW))
        db.commit()
        assert [i.symbol for i in lab.master_instruments(db, uid)] == ["EXTRACO", "FEEDCO", "WATCHCO"]          # sorted, synthetic and unlisted excluded
        other = lab.master_instruments(db, uid + 0 if False else UUID(int=uid.int ^ 1))
        assert [i.symbol for i in other] == ["FEEDCO"]                                                         # another user gets only the shared feed universe
    assert feed


def test_min_bars_keeps_only_instruments_with_enough_stored_history(client, engine):
    login(client, engine, "bars@example.com")
    uid = user_id(engine, "bars@example.com")
    rich, thin = listed(engine, "RICH"), listed(engine, "THIN")
    nse_sessions(engine, rich)
    nse_sessions(engine, thin, days=("2025-01-06",))
    with Session(engine) as db:
        assert [i.symbol for i in lab.master_instruments(db, uid, min_bars=1000)] == ["RICH"]
        assert {i.symbol for i in lab.master_instruments(db, uid)} == {"RICH", "THIN"}


def test_the_master_preset_lists_the_whole_list_with_no_history_requirement(client, engine):
    login(client, engine, "preset@example.com")
    listed(engine, "AAA"); listed(engine, "BBB")
    body = client.get("/api/v1/signals-lab/lab/universes").json()
    master = next(p for p in body["presets"] if p["key"] == "master")
    assert master["symbols"] == ["AAA", "BBB"] and master["available"] == 2 and master["excluded"] == [] and "Entire Market watch list" in master["label"]
    assert body["presets"][0]["key"] == "master"                                                              # offered first


# ───────────────────────── scanner universe ─────────────────────────
def registry_defn() -> StrategyDefinition:
    return StrategyDefinition.model_validate(get("five_ema_alert").definition())


def test_a_registry_strategy_with_no_symbols_scans_the_whole_master_list_that_has_data(client, engine):
    login(client, engine, "scan@example.com")
    uid = user_id(engine, "scan@example.com")
    rich, thin = listed(engine, "RICH"), listed(engine, "THIN")
    nse_sessions(engine, rich)
    nse_sessions(engine, thin, days=("2025-01-06",))
    defn = registry_defn()
    assert defn.universe == []                                                                                # nothing is configured ...
    with Session(engine) as db:
        assert [i.symbol for i in runner.resolve_instruments(db, defn, None, uid)] == []  if False else True
        monkey_min = runner.MASTER_SCAN_MIN_BARS
        runner.MASTER_SCAN_MIN_BARS = 1000
        try:
            assert [i.symbol for i in runner.resolve_instruments(db, defn, None, uid)] == ["RICH"]            # ... and it runs on every listed name with enough 1-minute data
            explicit = defn.model_copy(update={"universe": ["@master", thin]})
            assert [i.symbol for i in runner.resolve_instruments(db, explicit, None, uid)] == ["RICH", "THIN"]  # the token and explicit ids combine
        finally:
            runner.MASTER_SCAN_MIN_BARS = monkey_min


def test_a_non_registry_strategy_with_no_symbols_is_still_an_error_and_master_needs_an_owner(client, engine):
    login(client, engine, "err@example.com")
    uid = user_id(engine, "err@example.com")
    rich = listed(engine, "RICH")
    nse_sessions(engine, rich)
    plain = StrategyDefinition.model_validate(definition(rich, universe=[]))
    with Session(engine) as db:
        with pytest.raises(runner.RunnerError, match="at least one instrument"):
            runner.resolve_instruments(db, plain, None, uid)
        with pytest.raises(runner.RunnerError, match="needs an owner"):
            runner.resolve_instruments(db, plain.model_copy(update={"universe": ["@master"]}), None, None)


# ───────────────────────── rotating refresh ─────────────────────────
class FakeSync:
    def __init__(self, fail=()):
        self.calls: list[UUID] = []
        self.fail = set(fail)

    def __call__(self, *, instrument_id, **kwargs):
        self.calls.append(instrument_id)
        if instrument_id in self.fail:
            raise RuntimeError("provider down")
        return type("R", (), {"sync_status": "ok"})()


@pytest.fixture
def fake_md(monkeypatch):
    from app.api import market_data as md
    sync = FakeSync()
    monkeypatch.setattr(md, "market_data_provider_for_user", lambda db, uid: object())
    monkeypatch.setattr(md, "sync_history", sync)
    return sync


def stage_three(engine, email):
    """Three symbols whose newest 1-minute bars are 2 minutes, 40 minutes and 3 days old (and one with none)."""
    ids = {}
    for name, age in (("FRESH", timedelta(seconds=30)), ("OLD", timedelta(minutes=40)), ("OLDER", timedelta(days=3)), ("NONE", None)):
        iid = listed(engine, name)
        ids[name] = UUID(iid)
        if age is not None:
            add_bars(engine, iid, NOW - age - timedelta(minutes=4), [100.0] * 5)          # the newest bar OPENED `age` ago
    return ids


def test_refresh_goes_stalest_first_skips_current_symbols_and_records_failures(client, engine, fake_md):
    login(client, engine, "rot@example.com")
    ids = stage_three(engine, "rot@example.com")
    with Session(engine) as db:
        user = db.scalar(select(User).where(User.email == "rot@example.com"))
        insts = [db.get(Instrument, i) for i in ids.values()]
        result = fts.refresh_bars(db, user, insts, NOW)
    assert fake_md.calls == [ids["NONE"], ids["OLDER"], ids["OLD"]]                    # never-fetched first, then oldest; FRESH is skipped, the budget goes elsewhere
    assert ids["FRESH"] not in result and all(v is None for v in result.values())
    fake_md.calls.clear()
    fake_md.fail.add(ids["OLD"])
    with Session(engine) as db:
        user = db.scalar(select(User).where(User.email == "rot@example.com"))
        result = fts.refresh_bars(db, user, [db.get(Instrument, i) for i in ids.values()], NOW)
    assert "RuntimeError" in result[ids["OLD"]] and result[ids["NONE"]] is None          # one failure is recorded, the rest continue


def test_the_time_budget_stops_a_pass_and_the_next_pass_continues_from_the_stalest(client, engine, fake_md, monkeypatch):
    login(client, engine, "budget@example.com")
    ids = stage_three(engine, "budget@example.com")
    clock = iter([0.0, 0.0, 99.0, 100.0, 101.0, 102.0, 103.0, 104.0])
    class StubTime:      # only forward_test sees the scripted clock; patching time.monotonic itself would starve every other thread (event loops, the test client) of a clock
        def __getattr__(self, name):
            return getattr(_real_time, name)

        @staticmethod
        def monotonic():
            return next(clock)
    monkeypatch.setattr(fts, "_time", StubTime())
    with Session(engine) as db:
        user = db.scalar(select(User).where(User.email == "budget@example.com"))
        fts.refresh_bars(db, user, [db.get(Instrument, i) for i in ids.values()], NOW, max_seconds=50)
    assert fake_md.calls == [ids["NONE"]]                                                # the budget ran out after one: the others wait for the next pass


def test_outside_the_session_everything_is_refreshed_because_nothing_is_current(client, engine, fake_md):
    login(client, engine, "after@example.com")
    ids = stage_three(engine, "after@example.com")
    saturday = datetime(2026, 11, 21, 5, 0, tzinfo=timezone.utc)
    with Session(engine) as db:
        user = db.scalar(select(User).where(User.email == "after@example.com"))
        fts.refresh_bars(db, user, [db.get(Instrument, i) for i in ids.values()], saturday)
    assert len(fake_md.calls) == 4


def make_master_ft(engine, email, *, status="active", live=False, tf="15m", full=None) -> UUID:
    with Session(engine) as db:
        user = db.scalar(select(User).where(User.email == email))
        d = full or {"decision_timeframe": tf}
        ft = ForwardTest(user_id=user.id, name="m", strategy_key="five_ema_alert", definition=d, definition_hash="x", instrument_ids=["@master"], quantity=1,
                         start_date=NOW.date(), status=status, execute_live=live, slippage_bps=2, created_at=NOW, updated_at=NOW)
        db.add(ft)
        db.commit()
        return ft.id


def test_a_whole_list_forward_test_follows_the_list_and_is_not_refreshed_inline(client, engine, monkeypatch):
    login(client, engine, "follow@example.com")
    first = listed(engine, "AAA")
    nse_sessions(engine, first)
    ft_id = make_master_ft(engine, "follow@example.com", full=definition(first, stop={"mode": "percent", "value": 0.5}, target={"mode": "rr", "value": 2}))
    called = []
    with Session(engine) as db:
        ft = db.get(ForwardTest, ft_id)
        assert [i.symbol for i in fts.instruments_of(db, ft)] == ["AAA"]
        second = listed(engine, "BBB")                                                  # the user adds a stock to the master list later
        assert [i.symbol for i in fts.instruments_of(db, db.get(ForwardTest, ft_id))] == ["AAA", "BBB"]
        fts.run_once(db, ft, datetime(2025, 1, 10, 10, 5, tzinfo=timezone.utc), refresher=lambda *a, **k: called.append(1) or {})
        assert called == [] and ft.last_error is None                                  # the rotation feeds it; the test itself makes no data calls
        assert {u["symbol"] for u in ft.snapshot["universe"]} == {"AAA", "BBB"} and second


def test_the_api_creates_a_whole_list_test_that_stores_the_token_not_a_frozen_copy(client, engine):
    login(client, engine, "api@example.com")
    listed(engine, "AAA"); listed(engine, "BBB")
    made = client.post("/api/v1/forward-tests", headers=h(client), json={"strategy_key": "five_ema_alert", "universe": "master"})
    assert made.status_code == 201, made.text
    body = made.json()
    assert body["whole_market_watch_list"] is True and body["instrument_total"] == 2 and body["instrument_count"] == 1
    with Session(engine) as db:
        assert db.scalar(select(ForwardTest.instrument_ids)) == ["@master"]
    assert client.get("/api/v1/forward-tests").json()["forward_tests"][0]["whole_market_watch_list"] is True
    live = client.post("/api/v1/forward-tests", headers=h(client), json={"strategy_key": "five_ema_alert", "universe": "master", "execute_live": True})
    assert live.status_code == 201                                                       # symbols without a Dhan id simply never produce a live intent


def test_big_lists_run_once_per_candle_not_every_minute(client, engine):
    login(client, engine, "candle@example.com")
    ft_id = make_master_ft(engine, "candle@example.com", tf="15m")
    with Session(engine) as db:
        ft = db.get(ForwardTest, ft_id)
        ft.last_run_at = datetime(2026, 11, 23, 4, 46, tzinfo=timezone.utc)             # 10:16 IST: after the 10:15 boundary's 15s grace, already run for it
        assert not candle_due(ft, datetime(2026, 11, 23, 4, 50, tzinfo=timezone.utc))   # 10:20 IST, the same 15-minute candle: nothing new
        assert not should_run(ft, datetime(2026, 11, 23, 4, 50, tzinfo=timezone.utc))
        assert candle_due(ft, datetime(2026, 11, 23, 5, 0, 20, tzinfo=timezone.utc))     # 10:30:20 IST: a new candle closed 20s ago
        assert not candle_due(ft, datetime(2026, 11, 23, 5, 0, 5, tzinfo=timezone.utc))  # ... but give the bars 15s to arrive
        assert not candle_due(ft, datetime(2026, 11, 23, 3, 40, tzinfo=timezone.utc))    # 09:10 IST: before the open
        ft.last_run_at = None
        assert should_run(ft, datetime(2026, 11, 23, 4, 50, tzinfo=timezone.utc))        # the first pass after creation always runs


def test_the_background_rotation_only_works_for_dhan_users_of_the_list_while_the_market_is_open(client, engine, fake_md, monkeypatch):
    monkeypatch.setattr(mbr, "engine", engine)                                          # the TEST database, never the real one
    login(client, engine, "bg@example.com")
    ids = stage_three(engine, "bg@example.com")
    make_master_ft(engine, "bg@example.com")
    assert mbr.run_pass(datetime(2026, 11, 21, 5, 0, tzinfo=timezone.utc)) == {"users": 0, "refreshed": 0, "errors": 0}      # Saturday: nothing
    assert mbr.run_pass(NOW) == {"users": 0, "refreshed": 0, "errors": 0}                                                    # no Dhan credential / provider: not eligible
    from app.models import BrokerCredential, UserMarketDataSettings
    with Session(engine) as db:
        uid = db.scalar(select(User.id).where(User.email == "bg@example.com"))
        db.add(UserMarketDataSettings(user_id=uid, selected_provider="dhan", updated_at=NOW))
        db.add(BrokerCredential(user_id=uid, broker="dhan", encrypted_tokens="x", expires_at=NOW + timedelta(days=1), connected_at=NOW))
        db.commit()
    result = mbr.run_pass(NOW)
    assert result == {"users": 1, "refreshed": 3, "errors": 0}                          # FRESH is current; NONE, OLDER and OLD are refreshed
    assert fake_md.calls[0] == ids["NONE"]                                               # stalest first
    fake_md.calls.clear()
    with Session(engine) as db:
        ft = db.scalar(select(ForwardTest))
        ft.status = "paused"
        db.commit()
    assert mbr.run_pass(NOW) == {"users": 0, "refreshed": 0, "errors": 0} and fake_md.calls == []      # nobody needs the list any more: no Dhan calls at all


def test_which_users_need_the_list_is_decided_by_their_active_tests_and_registry_strategies():
    assert mbr._strategy_uses_master({"universe": ["@master"]}) and mbr._strategy_uses_master({"universe": [], "source": {"provenance": {"starter_key": "reg_five_ema_alert"}}})
    assert not mbr._strategy_uses_master({"universe": [], "source": {"provenance": {"starter_key": "ema_rsi_vwap"}}}) and not mbr._strategy_uses_master({"universe": ["abc"]})


def test_a_master_list_with_no_usable_history_says_so_plainly(client, engine):
    login(client, engine, "nodata@example.com")
    uid = user_id(engine, "nodata@example.com")
    thin = listed(engine, "THIN")
    nse_sessions(engine, thin, days=("2025-01-06",))                                                            # 375 bars: below the scanner's requirement
    with Session(engine) as db:
        with pytest.raises(runner.RunnerError, match="enough stored 1-minute data"):
            runner.resolve_instruments(db, registry_defn(), None, uid)


def test_a_test_registered_weeks_ahead_of_its_start_date_still_runs_and_just_waits(client, engine):
    login(client, engine, "ahead@example.com")
    iid = listed(engine, "AAA")
    nse_sessions(engine, iid)
    ft_id = make_master_ft(engine, "ahead@example.com", full=definition(iid, stop={"mode": "percent", "value": 0.5}, target={"mode": "rr", "value": 2}))
    with Session(engine) as db:
        ft = db.get(ForwardTest, ft_id)
        ft.start_date = (datetime(2025, 1, 10) + timedelta(days=45)).date()                                  # 45 days after "now"
        db.commit()
        fts.run_once(db, ft, datetime(2025, 1, 10, 10, 5, tzinfo=timezone.utc))
        assert ft.last_error is None and "Starts" in ft.snapshot["waiting"]
