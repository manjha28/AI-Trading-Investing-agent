"""Crypto (Delta) research system: data, indicators, strategies, backtest engine, API.

All candles below are TEST DATA generated deterministically in this file. They never flow into a runtime path.
"""
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.crypto_delta.backtest import ContractSpec, run_backtest
from app.crypto_delta.candles import RESOLUTION_SECONDS, Candle, CandleDataError, find_gaps, parse_candles, sync_candles
from app.crypto_delta.indicators import atr_series, ema_series, rsi_series
from app.crypto_delta.strategies import STRATEGIES, ParamError, Strategy, resolve_params
from app.db import Base, get_db
from app.delta_research.store import DeltaResearchStore
from app.main import app
from app.models import User
from app.models.crypto_delta import CryptoBacktestRun, CryptoCandle
from app.services.indicators import atr as shared_atr, ema as shared_ema, rsi as shared_rsi

D = Decimal
T0 = datetime(2026, 1, 5, 0, 0, tzinfo=timezone.utc)
SPEC = ContractSpec("TESTUSD", D("1"), D("0.5"), D("0.001"), D("0.0002"))


def candle(i: int, o, h, l, c, *, res: str = "1m", v="10") -> Candle:
    return Candle(T0 + timedelta(seconds=RESOLUTION_SECONDS[res] * i), D(str(o)), D(str(h)), D(str(l)), D(str(c)), D(v))


def wave(n: int, res: str = "15m", base: int = 100) -> list[Candle]:
    """TEST DATA: a deterministic triangular wave (period 40) with a small range per bar."""
    out = []
    for k in range(n):
        close = base + abs((k % 40) - 20)
        prior = base + abs(((k - 1) % 40) - 20)
        out.append(candle(k, prior, max(prior, close) + 1, min(prior, close) - 1, close, res=res))
    return out


# ---- indicators: parity with the shared library ----------------------------------------------

def test_series_match_the_shared_indicator_library():
    bars = wave(200)
    closes = [b.close for b in bars]
    assert ema_series(closes, 12)[-1] == shared_ema(bars, 12).value
    assert ema_series(closes, 12)[10] is None and ema_series(closes, 12)[11] is not None
    assert rsi_series(closes, 14)[-1] == shared_rsi(bars, 14).value
    assert rsi_series(closes, 14)[13] is None and rsi_series(closes, 14)[14] is not None
    atr = atr_series([b.high for b in bars], [b.low for b in bars], closes, 14)
    assert atr[-1] == shared_atr(bars, 14).value and atr[13] is None and atr[14] is not None
    # an intermediate bar must agree too (incremental == recompute from scratch)
    assert rsi_series(closes, 14)[120] == shared_rsi(bars[:121], 14).value


# ---- candles: parsing, completed-only storage, gaps ------------------------------------------

def payload(rows):
    return {"success": True, "result": rows}


def test_parse_candles_is_strict_and_keeps_exact_decimals():
    text = '{"success":true,"result":[{"time":1795181580,"open":61427.5,"high":61448.0,"low":61361.5,"close":61365.5,"volume":14953.0},' \
           '{"time":1795181640,"open":1,"high":0.5,"low":2,"close":1,"volume":1},{"time":1795181700,"open":null,"high":1,"low":1,"close":1,"volume":1}]}'
    candles, rejected = parse_candles(json.loads(text, parse_float=D))
    assert len(candles) == 1 and rejected == 2  # high<low and a null price are refused, not repaired
    assert candles[0].open == D("61427.5") and candles[0].open_time == datetime.fromtimestamp(1795181580, timezone.utc)
    with pytest.raises(CandleDataError):
        parse_candles({"success": False})


@pytest.fixture
def engine():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    yield eng
    Base.metadata.drop_all(eng)
    eng.dispose()


def mock_delta(rows_by_call: list[list[dict]]):
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(dict(request.url.params))
        rows = rows_by_call[min(len(calls) - 1, len(rows_by_call) - 1)]
        return httpx.Response(200, text=json.dumps(payload(rows)))

    return httpx.MockTransport(handler), calls


def row(epoch: int, price: str = "100"):
    return {"time": epoch, "open": float(price), "high": float(price) + 1, "low": float(price) - 1, "close": float(price), "volume": 5.0}


def test_sync_stores_only_completed_candles_paginates_and_is_idempotent(engine):
    now = datetime(2026, 1, 5, 10, 7, 30, tzinfo=timezone.utc)  # the 10:07 candle is still forming
    base = int(datetime(2026, 1, 5, 10, 0, tzinfo=timezone.utc).timestamp())
    transport, calls = mock_delta([[row(base + 60 * k) for k in range(8)]])  # 10:00 .. 10:07
    with Session(engine) as db:
        first = sync_candles(db, "TESTUSD", "1m", datetime(2026, 1, 5, 10, 0, tzinfo=timezone.utc), now, now=now, transport=transport)
        assert first == {"fetched": 8, "incomplete_skipped": 1, "rejected": 0, "inserted": 7}
        again = sync_candles(db, "TESTUSD", "1m", datetime(2026, 1, 5, 10, 0, tzinfo=timezone.utc), now, now=now, transport=transport)
        assert again["inserted"] == 0
        assert db.scalar(select(func.count(CryptoCandle.id))) == 7
        assert all(isinstance(row.close, Decimal) for row in db.scalars(select(CryptoCandle)))
    # a 5000-minute window needs three requests (2000 candles per call)
    wide, wide_calls = mock_delta([[]])
    with Session(engine) as db:
        sync_candles(db, "TESTUSD", "1m", now - timedelta(minutes=5000), now, now=now, transport=wide)
    assert len(wide_calls) == 3 and all(int(c["end"]) - int(c["start"]) <= 2000 * 60 for c in wide_calls)


def test_gaps_are_reported_not_bridged():
    series = [candle(0, 1, 2, 1, 1), candle(1, 1, 2, 1, 1), candle(5, 1, 2, 1, 1)]
    assert find_gaps(series, "1m") == [(series[1].open_time, series[2].open_time, 3)]


# ---- strategies -------------------------------------------------------------------------------

def jumpy(n: int, res: str = "15m") -> list[Candle]:
    """TEST DATA: an uneven series (drift plus jumps) so channel breakouts actually occur."""
    closes = [100 + (k * 7) % 23 + k // 3 - (k // 50) * 20 for k in range(n)]
    return [candle(k, c, c + 1, c - 1, c, res=res) for k, c in enumerate(closes)]


def test_strategies_emit_targets_from_data_up_to_each_bar_only():
    bars = wave(300)
    overrides = {"donchian_breakout": {"entry_period": 10, "exit_period": 5}}  # channel shorter than the test wave
    for strategy in STRATEGIES.values():
        params = resolve_params(strategy, overrides.get(strategy.id, {}))
        bars = jumpy(300) if strategy.id == "donchian_breakout" else wave(300)
        full = strategy.targets(bars, params)
        # no look-ahead: truncating the future must not change any earlier target
        assert strategy.targets(bars[:150], params) == full[:150], strategy.id
        assert set(full) <= {-1, 0, 1} and any(full), strategy.id
    cross = STRATEGIES["ema_cross"]
    assert set(cross.targets(bars, resolve_params(cross, {"allow_short": False}))) <= {0, 1}


def test_parameters_are_validated():
    s = STRATEGIES["ema_cross"]
    assert resolve_params(s, {})["fast_period"] == 12
    for bad in ({"fast_period": 50, "slow_period": 20}, {"fast_period": 1}, {"fast_period": 2.5}, {"nope": 1}, {"stop_atr_mult": "abc"}, {"allow_short": "yes"}):
        with pytest.raises(ParamError):
            resolve_params(s, bad)
    with pytest.raises(ParamError):
        resolve_params(STRATEGIES["rsi_reversion"], {"oversold": 60})


# ---- engine: hand-computed scenarios ---------------------------------------------------------

def fixed(targets: list[int]) -> Strategy:
    return Strategy("fixed", "fixed", "", [], lambda candles, params: targets, lambda p: 0)


P = {"stop_atr_mult": D("0"), "take_profit_r": D("0"), "atr_period": 2, "allow_short": True}


def run(candles, targets, params=None, **kw):
    return run_backtest(candles, "1m", fixed(targets), params or P, SPEC, initial_capital=D("1000"), contracts=D("2"), slippage_bps=kw.get("slip", D("0")))


def test_signal_at_close_fills_at_next_open_and_pays_taker_fees_both_ways():
    bars = [candle(0, 100, 101, 99, 100), candle(1, 102, 103, 101, 103), candle(2, 110, 111, 109, 110), candle(3, 108, 109, 107, 108)]
    result = run(bars, [1, 1, 0, 0])  # long decided at close of bar0; flat decided at close of bar2
    trade = result["trades"][0]
    assert (trade["entry_price"], trade["exit_price"]) == ("102.0", "108.0")  # next opens, not the signal bars' closes
    assert trade["side"] == "long" and trade["exit_reason"] == "signal"
    # gross = (108-102) * 2 contracts * 1 = 12; fees = 0.001 * 2 * (102 + 108) = 0.42 ; net 11.58
    assert (trade["gross_pnl"], trade["fees"], trade["net_pnl"]) == ("12.00", "0.42", "11.58")
    m = result["metrics"]
    assert m["net_pnl"] == "11.58" and m["win_rate_pct"] == "100.00" and m["profit_factor"] is None and m["closed_trades"] == 1
    assert result["hypothetical"] is True and "not_modelled" in result["assumptions"]


def test_short_pnl_and_slippage_are_adverse():
    bars = [candle(0, 100, 101, 99, 100), candle(1, 100, 101, 99, 100), candle(2, 90, 91, 89, 90), candle(3, 90, 91, 89, 90)]
    flat = run(bars, [-1, -1, 0, 0])["trades"][0]
    assert flat["side"] == "short" and flat["gross_pnl"] == "20.00"  # (100-90)*2
    slipped = run(bars, [-1, -1, 0, 0], slip=D("100"))["trades"][0]  # 1% worse on both fills
    # the buy-back is slipped to 90.9 and displayed at the 0.5 tick as 91.0; P&L uses the unrounded price
    assert slipped["entry_price"] == "99.0" and slipped["exit_price"] == "91.0" and D(slipped["net_pnl"]) < D(flat["net_pnl"])


def test_stop_hits_before_target_gaps_fill_at_open_and_reentry_is_blocked():
    p = {**P, "stop_atr_mult": D("1"), "take_profit_r": D("2")}
    warm = [candle(i, 100, 102, 98, 100) for i in range(4)]  # ATR(2) = 4
    entry = candle(4, 100, 101, 99, 100)
    # bar 5 trades through BOTH the stop (96) and the target (108): the stop is assumed first
    both = run([*warm, entry, candle(5, 100, 109, 95, 100), candle(6, 100, 101, 99, 100)], [0, 0, 0, 1, 1, 1, 1], p)
    stop_trade = both["trades"][0]
    assert stop_trade["exit_reason"] == "stop" and stop_trade["exit_price"] == "96.0"
    assert len(both["trades"]) == 1  # target still long, but re-entry is blocked until the signal resets
    # a gap that opens beyond the stop fills at the open, not at the stop level
    gap = run([*warm, entry, candle(5, 90, 91, 89, 90)], [0, 0, 0, 1, 1, 1], p)["trades"][0]
    assert gap["exit_reason"] == "stop" and gap["exit_price"] == "90.0"
    hit_target = run([*warm, entry, candle(5, 100, 109, 99, 105)], [0, 0, 0, 1, 1, 1], p)["trades"][0]
    assert hit_target["exit_reason"] == "target" and hit_target["exit_price"] == "108.0"


def test_missing_candles_close_the_position_and_end_of_data_is_marked():
    bars = [candle(0, 100, 101, 99, 100), candle(1, 100, 101, 99, 100), candle(2, 100, 101, 99, 101), candle(9, 120, 121, 119, 120), candle(10, 120, 121, 119, 120)]
    result = run(bars, [1, 1, 1, 1, 1])
    assert [t["exit_reason"] for t in result["trades"]] == ["data_gap", "end_of_data"] or result["trades"][0]["exit_reason"] == "data_gap"
    assert result["trades"][0]["exit_price"] == "101.0"  # last close before the gap, never a price inside it
    assert result["metrics"]["data_gap_exits"] == 1
    # equity is consistent: initial capital + sum of net pnl
    assert D(result["equity"][-1]["equity"]) == D("1000") + sum(D(t["net_pnl"]) for t in result["trades"])


def test_max_drawdown_and_no_trades_are_reported_honestly():
    bars = [candle(0, 100, 100, 100, 100), candle(1, 100, 100, 100, 100)]
    empty = run(bars, [0, 0])
    assert empty["metrics"]["closed_trades"] == 0 and empty["metrics"]["net_pnl"] is None and empty["metrics"]["win_rate_pct"] is None
    losing = run([candle(0, 100, 100, 100, 100), candle(1, 100, 100, 100, 100), candle(2, 90, 90, 90, 90), candle(3, 90, 90, 90, 90)], [1, 1, 0, 0])
    assert losing["metrics"]["max_drawdown"] != "0.00" and losing["metrics"]["wins"] == 0


# ---- API ---------------------------------------------------------------------------------------

@pytest.fixture
def client(engine) -> Iterator[TestClient]:
    def override_get_db():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def login(client: TestClient, engine, email: str) -> dict[str, str]:
    creds = {"email": email, "password": "correct-horse-battery"}
    assert client.post("/api/v1/auth/register", json=creds).status_code == 202
    assert client.post("/api/v1/auth/login", json=creds).status_code == 200
    with Session(engine) as db:
        db.scalar(select(User).where(User.email == email)).email_verified = True
        db.commit()
    return {"X-CSRF-Token": client.cookies.get("csrf_token", "")}


@pytest.fixture
def delta_store(tmp_path: Path, monkeypatch) -> Path:
    """TEST DATA ONLY: a throwaway product list with one USD perpetual and one non-USD contract."""
    path = tmp_path / "delta_test.sqlite3"
    with DeltaResearchStore(path) as store:
        store.save_products([
            {"id": 1, "symbol": "TESTUSD", "contract_type": "perpetual_futures", "state": "live", "contract_value": "0.001",
             "tick_size": "0.5", "taker_commission_rate": "0.0005", "maker_commission_rate": "0.0002", "quoting_asset": {"symbol": "USD"},
             "initial_margin": "0.5", "maintenance_margin": "0.25", "default_leverage": "200", "position_size_limit": 5000, "liquidation_penalty_factor": "0.5"},
            {"id": 2, "symbol": "ODDUSDT", "contract_type": "perpetual_futures", "state": "live", "contract_value": "1",
             "taker_commission_rate": "0.0005", "maker_commission_rate": "0.0002", "quoting_asset": {"symbol": "USDT"}},
            {"id": 3, "symbol": "NOFEE", "contract_type": "perpetual_futures", "state": "live", "contract_value": "1", "quoting_asset": {"symbol": "USD"}},
        ])
    monkeypatch.setattr(settings, "delta_research_enabled", True)
    monkeypatch.setattr(settings, "delta_research_db_path", str(path))
    return path


def seed_candles(engine, symbol="TESTUSD", resolution="15m", bars=330) -> None:
    """TEST DATA: completed 15m candles ending at the last closed boundary."""
    step = RESOLUTION_SECONDS[resolution]
    end = datetime.fromtimestamp(int(datetime.now(timezone.utc).timestamp()) // step * step, timezone.utc)
    with Session(engine) as db:
        for k, bar in enumerate(wave(bars, resolution)):
            db.add(CryptoCandle(symbol=symbol, resolution=resolution, open_time=end - timedelta(seconds=step * (bars - k)),
                                open=bar.open, high=bar.high, low=bar.low, close=bar.close, volume=bar.volume,
                                source="delta_rest_history", fetched_at=end))
        db.commit()


BODY = {"strategy_id": "ema_cross", "symbol": "TESTUSD", "resolution": "15m", "days": 3, "params": {"stop_atr_mult": "2"},
        "contracts": "10", "initial_capital": "1000", "slippage_bps": "1"}


def test_backtest_api_runs_on_stored_candles_and_stores_a_labelled_hypothetical_result(client, engine, delta_store):
    headers = login(client, engine, "crypto@example.com")
    seed_candles(engine)
    response = client.post("/api/v1/crypto-delta/backtests", headers=headers, json=BODY)
    assert response.status_code == 200, response.text
    run = response.json()
    assert run["status"] == "completed" and run["hypothetical"] is True and "Not a position" in run["label"]
    assert run["assumptions"]["fee_rate_per_fill"] == "0.0005" and run["assumptions"]["contract_value"] == "0.001"
    assert "funding payments" in run["assumptions"]["not_modelled"]
    quality = run["data_quality"]
    assert quality["bars"] == run["bars_used"] and quality["gaps"] == 0 and quality["downloaded"] is False
    assert run["metrics"]["closed_trades"] > 0 and len(run["trades"]) == run["metrics"]["closed_trades"]
    assert run["trades"][0]["net_pnl"] and run["equity"]
    listing = client.get("/api/v1/crypto-delta/backtests").json()
    assert [item["id"] for item in listing] == [run["id"]] and "trades" not in listing[0]
    assert client.get(f"/api/v1/crypto-delta/backtests/{run['id']}").json()["trades"] == run["trades"]


def test_backtest_results_are_owner_scoped(client, engine, delta_store):
    headers = login(client, engine, "owner@example.com")
    seed_candles(engine)
    run_id = client.post("/api/v1/crypto-delta/backtests", headers=headers, json=BODY).json()["id"]
    client.post("/api/v1/auth/logout", headers=headers)
    login(client, engine, "other@example.com")
    assert client.get("/api/v1/crypto-delta/backtests").json() == []
    assert client.get(f"/api/v1/crypto-delta/backtests/{run_id}").status_code == 404


def test_backtest_validation_and_fail_closed_cases(client, engine, delta_store, monkeypatch):
    headers = login(client, engine, "crypto@example.com")
    seed_candles(engine)
    post = lambda **changes: client.post("/api/v1/crypto-delta/backtests", headers=headers, json={**BODY, **changes})  # noqa: E731
    assert post(strategy_id="nope").status_code == 404
    assert post(params={"fast_period": 99, "slow_period": 10}).status_code == 422
    assert post(contracts=2.5).status_code == 422  # floats are refused
    assert post(days=999).status_code == 422 and post(resolution="7m").status_code == 422
    assert post(symbol="ODDUSDT").status_code == 409  # not USD: no FX is applied
    assert post(symbol="NOFEE").status_code == 409  # fee rates unknown: never assumed
    assert post(symbol="MISSINGUSD").status_code == 404
    assert client.post("/api/v1/crypto-delta/backtests", json=BODY).status_code == 403  # CSRF
    # no stored candles and the public service unreachable -> a clear no-data answer, nothing invented
    monkeypatch.setattr("app.api.crypto_delta.sync_candles", lambda *a, **k: (_ for _ in ()).throw(CandleDataError("offline")))
    empty = post(symbol="TESTUSD", resolution="5m", days=2)
    assert empty.status_code == 409 and "needs at least" in empty.json()["detail"]
    with Session(engine) as db:
        stored = db.scalar(select(CryptoBacktestRun).where(CryptoBacktestRun.status == "no_data"))
        assert stored is not None and stored.data_quality["download_error"] == "offline"
    monkeypatch.setattr(settings, "delta_research_enabled", False)
    assert post().status_code == 503


def test_candles_strategies_and_contract_endpoints(client, engine, delta_store):
    login(client, engine, "crypto@example.com")
    seed_candles(engine, bars=120)
    spec = client.get("/api/v1/crypto-delta/contracts/testusd").json()
    assert spec["taker_fee_rate"] == "0.0005" and spec["quote_currency"] == "USD" and spec["source"] == "Delta Exchange product data"
    strategies = client.get("/api/v1/crypto-delta/strategies").json()
    assert {s["id"] for s in strategies["strategies"]} == {"ema_cross", "rsi_reversion", "donchian_breakout"}
    assert "15m" in strategies["resolutions"]
    stored = client.get("/api/v1/crypto-delta/candles", params={"symbol": "testusd", "resolution": "15m", "limit": 100}).json()
    assert stored["count"] == 100 and stored["source"] == "delta_rest_history" and stored["candles"][0]["open"]
    assert client.get("/api/v1/crypto-delta/candles", params={"symbol": "TESTUSD", "resolution": "9m"}).status_code == 422


def test_candles_can_be_read_and_redownloaded_for_an_exact_window(client, engine, delta_store, monkeypatch):
    """A stored backtest charts its own window; if the candles are gone they can be fetched again."""
    headers = login(client, engine, "crypto@example.com")
    seed_candles(engine, bars=100)
    all_candles = client.get("/api/v1/crypto-delta/candles", params={"symbol": "TESTUSD", "resolution": "15m", "limit": 100}).json()["candles"]
    first, last = all_candles[10]["timestamp"], all_candles[19]["timestamp"]
    window = client.get("/api/v1/crypto-delta/candles", params={"symbol": "TESTUSD", "resolution": "15m", "start": first, "end": last, "limit": 500}).json()
    assert [c["timestamp"] for c in window["candles"]] == [c["timestamp"] for c in all_candles[10:19]]  # end is exclusive
    assert client.get("/api/v1/crypto-delta/candles", params={"symbol": "TESTUSD", "resolution": "15m", "start": first}).status_code == 422
    assert client.get("/api/v1/crypto-delta/candles", params={"symbol": "TESTUSD", "resolution": "15m", "start": last, "end": first}).status_code == 422

    with Session(engine) as db:  # the stored candles disappear...
        db.query(CryptoCandle).delete()
        db.commit()
    calls = []

    def fake_sync(db, symbol, resolution, start, end, **kwargs):  # ...and are downloaded again for exactly that window
        calls.append((symbol, resolution, start, end))
        return {"fetched": 9, "incomplete_skipped": 0, "rejected": 0, "inserted": 9}

    monkeypatch.setattr("app.api.crypto_delta.sync_candles", fake_sync)
    body = {"symbol": "TESTUSD", "resolution": "15m", "start": first, "end": last}
    assert client.post("/api/v1/crypto-delta/candles/sync", headers=headers, json=body).status_code == 200
    assert calls and calls[0][0:2] == ("TESTUSD", "15m") and calls[0][2].isoformat().startswith(first[:16])
    assert client.post("/api/v1/crypto-delta/candles/sync", headers=headers, json={**body, "days": 3}).status_code == 422  # ambiguous
    assert client.post("/api/v1/crypto-delta/candles/sync", headers=headers, json={"symbol": "TESTUSD", "resolution": "15m"}).status_code == 422
    too_long = {**body, "start": "2025-01-01T00:00:00Z"}
    assert client.post("/api/v1/crypto-delta/candles/sync", headers=headers, json=too_long).status_code == 422


# ---- margin, leverage, sizing, liquidation (isolated margin at the chosen leverage) ---------------------------------------------
MARGIN_SPEC = ContractSpec("MARGUSD", D("1"), D("0.5"), D("0.001"), D("0.0002"), initial_margin_pct=D("1"), maintenance_margin_pct=D("0.5"),
                           default_leverage=D("100"), liquidation_penalty_factor=D("0.5"), position_size_limit=500)
FLAT = {"stop_atr_mult": D("0"), "take_profit_r": D("0"), "atr_period": 2, "allow_short": True}


def margin_run(bars, targets, params=None, spec=MARGIN_SPEC, capital="1000", contracts="2", **kw):
    return run_backtest(bars, "1m", fixed(targets), params or FLAT, spec, initial_capital=D(capital), contracts=D(contracts), slippage_bps=kw.pop("slip", D("0")), **kw)


def test_max_leverage_comes_from_the_exchange_initial_margin():
    assert MARGIN_SPEC.max_leverage == D("100")
    assert ContractSpec("X", D("1"), None, D("0"), D("0")).max_leverage is None            # no margin data -> no claim
    for bad in (D("101"), D("0.5")):
        with pytest.raises(ValueError, match="Leverage must be between 1 and 100"):
            margin_run([candle(i, 100, 100, 100, 100) for i in range(4)], [1, 1, 0, 0], leverage=bad)


def test_a_position_is_liquidated_at_its_liquidation_price_and_the_loss_is_bounded_at_the_margin():
    # 10x: liquidation distance = 1/10 - 0.5% = 9.5% -> long entered at 100 is liquidated at 90.5
    bars = [candle(0, 100, 100, 100, 100), candle(1, 100, 100, 100, 100), candle(2, 100, 100, 85, 86), candle(3, 86, 86, 86, 86), candle(4, 86, 86, 86, 86)]
    result = margin_run(bars, [1, 1, 1, 1, 1], leverage=D("10"), contracts="10")
    trade = result["trades"][0]
    assert trade["exit_reason"] == "liquidated" and trade["exit_price"] == "90.5" and trade["leverage"] == "10"
    notional, margin = D("10") * 100, D("10") * 100 / 10                     # 1000 notional, 100 margin
    maintenance = notional * D("0.005")                                      # 5
    expected_fees = D("0.001") * 10 * 100 + D("0.001") * 10 * D("90.5") + D("0.5") * maintenance   # entry + exit taker + penalty on the maintenance margin
    assert D(trade["gross_pnl"]) == -(margin - maintenance) and D(trade["fees"]) == expected_fees.quantize(D("0.01"))
    assert D(trade["net_pnl"]) >= -margin - D("5")                           # never worse than the margin (plus fees/penalty)
    assert result["metrics"]["liquidations"] == 1 and D(result["equity"][-1]["equity"]) > 0
    assert result["metrics"]["max_margin_used"] == "100.00"
    assert result["assumptions"]["margin"]["model"].startswith("isolated margin") and result["assumptions"]["margin"]["leverage"] == "10"


def test_equity_can_never_go_negative_and_drawdown_stays_within_100_percent_however_bad_the_run():
    # the reported bug: a fixed contract count and no margin model let losses exceed the starting capital
    crash = [candle(i, 100 - 4 * i, 100 - 4 * i, 100 - 4 * i - 3, 100 - 4 * i - 3) for i in range(20)]
    legacy = run(crash, [1] * 20)                                            # SPEC has no margin data: the legacy path
    assert legacy["assumptions"]["margin"]["model"].startswith("none")
    wild = [candle(i, 100, 130 if i % 2 else 100, 60 if i % 2 == 0 else 100, 100 if i % 2 else 60) for i in range(60)]
    result = margin_run(wild, [-1, 1] * 30, leverage=D("50"), contracts="90", capital="1000", slip=D("5"))   # always on the losing side of each swing
    equities = [D(p["equity"]) for p in result["equity"]]
    assert min(equities) >= 0
    assert D(result["metrics"]["max_drawdown_pct"]) <= D("100")
    assert result["metrics"]["liquidations"] >= 1


def test_entries_without_enough_margin_are_skipped_and_counted_not_forced():
    bars = [candle(i, 100, 101, 99, 100) for i in range(6)]
    # 1000 capital at 10x supports ~99 contracts at price 100; asking for 200 must not open a position
    skipped = margin_run(bars, [1] * 6, leverage=D("10"), contracts="200", capital="1000")
    assert skipped["trades"] == [] and skipped["metrics"]["skipped_entries_margin"] >= 1
    ok = margin_run(bars, [1] * 6, leverage=D("10"), contracts="50", capital="1000")
    assert ok["trades"] and ok["metrics"]["skipped_entries_margin"] == 0


def test_sizing_by_notional_and_by_risk_uses_whole_contracts_capped_by_margin_and_position_limit():
    spec = ContractSpec("SMALLUSD", D("0.001"), D("0.5"), D("0.0005"), D("0.0002"), initial_margin_pct=D("0.5"), maintenance_margin_pct=D("0.25"),
                        default_leverage=D("200"), liquidation_penalty_factor=D("0.5"), position_size_limit=100)
    bars = [candle(i, 50000, 50010, 49990, 50000) for i in range(8)]
    notional = margin_run(bars, [1] * 8, spec=spec, leverage=D("20"), sizing_mode="notional_usd", sizing_value=D("1000"), capital="5000")
    # $1000 / (0.001 BTC x $50,000 = $50 per contract) = 20 contracts
    assert notional["trades"][0]["contracts"] == "20" and notional["trades"][0]["notional_usd"] == "1000.00"
    params = {**FLAT, "stop_atr_mult": D("1")}
    risk = margin_run([candle(i, 50000, 50020, 49980, 50000) for i in range(8)], [1] * 8, params, spec=spec, leverage=D("20"), sizing_mode="risk_pct", sizing_value=D("1"), capital="5000")
    # ATR(2) = 40 -> stop distance 40; risking 1% of 5000 = $50 -> 50 / (40 x 0.001) = 1250 contracts -> capped by the 100-contract position limit
    assert risk["trades"][0]["contracts"] == "100"
    tiny = margin_run(bars, [1] * 8, spec=spec, leverage=D("20"), sizing_mode="notional_usd", sizing_value=D("10"), capital="5000")
    assert tiny["trades"] == [] and tiny["metrics"]["skipped_entries_size"] >= 1           # $10 is less than one $50 contract
    with pytest.raises(ValueError, match="needs a stop"):
        margin_run(bars, [1] * 8, spec=spec, sizing_mode="risk_pct", sizing_value=D("1"))
    with pytest.raises(ValueError, match="sizing_mode"):
        margin_run(bars, [1] * 8, spec=spec, sizing_mode="bogus")


def test_stop_inside_the_liquidation_distance_is_hit_first_but_a_stop_beyond_it_is_never_reached():
    warm = [candle(i, 100, 102, 98, 100) for i in range(4)]                       # ATR(2) = 4
    entry = candle(4, 100, 101, 99, 100)
    crash = candle(5, 100, 100, 80, 85)                                           # trades through 96 (stop 1 ATR) and 90.5 (10x liquidation)
    near_stop = margin_run([*warm, entry, crash], [0, 0, 0, 1, 1, 1], {**FLAT, "stop_atr_mult": D("1")}, leverage=D("10"), contracts="10")["trades"][0]
    assert near_stop["exit_reason"] == "stop" and near_stop["exit_price"] == "96.0"
    far_stop = margin_run([*warm, entry, crash], [0, 0, 0, 1, 1, 1], {**FLAT, "stop_atr_mult": D("4")}, leverage=D("10"), contracts="10")["trades"][0]   # stop at 84
    assert far_stop["exit_reason"] == "liquidated" and far_stop["exit_price"] == "90.5"


def test_short_liquidation_mirrors_the_long_and_a_gap_through_it_fills_at_the_open_but_the_loss_stays_bounded():
    bars = [candle(0, 100, 100, 100, 100), candle(1, 100, 100, 100, 100), candle(2, 120, 125, 119, 124), candle(3, 124, 124, 124, 124)]
    trade = margin_run(bars, [-1] * 4, leverage=D("10"), contracts="10")["trades"][0]
    assert trade["side"] == "short" and trade["exit_reason"] == "liquidated"
    assert trade["exit_price"] == "120.0"                                          # gapped past the 109.5 liquidation price: filled at the open
    assert D(trade["net_pnl"]) >= -(D("100") + D("5"))                             # still bounded at the margin plus fees/penalty


def test_fees_are_charged_per_fill_on_each_trades_own_size():
    bars = [candle(0, 100, 100, 100, 100), candle(1, 100, 100, 100, 100), candle(2, 110, 110, 110, 110), candle(3, 110, 110, 110, 110)]
    trade = margin_run(bars, [1, 1, 0, 0], leverage=D("10"), contracts="7")["trades"][0]
    assert trade["contracts"] == "7" and trade["fees"] == "1.47"                   # 0.001 x 7 x (100 + 110)
    assert trade["gross_pnl"] == "70.00" and trade["net_pnl"] == "68.53"


# ---- API: margin data, validation, per-run settings, delete ------------------------------------------------------------------
def test_contract_info_publishes_lot_size_margin_and_leverage_limits(client, engine, delta_store):
    headers = login(client, engine, "info@example.com")
    info = client.get("/api/v1/crypto-delta/contracts/TESTUSD").json()
    assert info["units_per_contract"] == "0.001" and info["initial_margin_pct"] == "0.5" and info["maintenance_margin_pct"] == "0.25"
    assert info["max_leverage"] == "200" and info["position_size_limit"] == 5000 and info["liquidation_penalty_factor"] == "0.5"
    assert headers


def test_backtest_api_applies_leverage_sizing_and_records_every_setting_with_the_run(client, engine, delta_store):
    headers = login(client, engine, "settings@example.com")
    seed_candles(engine)
    body = {**BODY, "leverage": "20", "sizing_mode": "notional_usd", "notional_usd": "500", "initial_capital": "2000"}
    run = client.post("/api/v1/crypto-delta/backtests", headers=headers, json=body).json()
    margin = run["assumptions"]["margin"]
    assert margin["leverage"] == "20" and margin["max_leverage"] == "200" and margin["initial_margin_pct"] == "0.5" and margin["position_size_limit"] == 5000
    assert run["assumptions"]["sizing_mode"] == "notional_usd" and run["assumptions"]["sizing_value"] == "500" and run["assumptions"]["initial_capital_usd"] == "2000"
    assert run["params"] and "stop_atr_mult" in run["params"]                      # the strategy parameters travel with the run
    assert "liquidations" in run["metrics"] and D(run["metrics"]["max_drawdown_pct"]) <= 100 and min(D(p["equity"]) for p in run["equity"]) >= 0
    unlevered = client.post("/api/v1/crypto-delta/backtests", headers=headers, json={**body, "leverage": "1"}).json()
    assert unlevered["metrics"]["liquidations"] == 0                                # 1x leaves ~99.5% room: nothing in this data liquidates it
    for trade in run["trades"]:
        assert trade["leverage"] == "20" and D(trade["notional_usd"]) <= D("500") + D("1")


def test_backtest_api_refuses_settings_the_contract_does_not_allow(client, engine, delta_store):
    headers = login(client, engine, "limits@example.com")
    seed_candles(engine)
    post = lambda **extra: client.post("/api/v1/crypto-delta/backtests", headers=headers, json={**BODY, **extra})   # noqa: E731
    over = post(leverage="250")
    assert over.status_code == 422 and "between 1 and 200" in over.json()["detail"]
    assert post(contracts="5001").status_code == 422                                   # above the contract's own position-size limit
    missing = post(sizing_mode="notional_usd")
    assert missing.status_code == 422 and "notional_usd" in missing.json()["detail"]
    assert post(sizing_mode="risk_pct", risk_pct="1", params={"stop_atr_mult": "0"}).status_code == 422     # risk sizing needs a stop
    assert post(leverage="0.5").status_code == 422


def test_a_contract_without_margin_data_is_refused_not_assumed(client, engine, delta_store):
    headers = login(client, engine, "nomargin@example.com")
    with DeltaResearchStore(delta_store) as store:
        store.save_products([{"id": 9, "symbol": "NOMARGIN", "contract_type": "perpetual_futures", "state": "live", "contract_value": "1", "tick_size": "0.1",
                              "taker_commission_rate": "0.0005", "maker_commission_rate": "0.0002", "quoting_asset": {"symbol": "USD"}}])
    response = client.post("/api/v1/crypto-delta/backtests", headers=headers, json={**BODY, "symbol": "NOMARGIN"})
    assert response.status_code == 409 and "Margin data" in response.json()["detail"]


def test_a_stored_backtest_can_be_deleted_by_its_owner_only(client, engine, delta_store):
    headers = login(client, engine, "deleter@example.com")
    seed_candles(engine)
    keep = client.post("/api/v1/crypto-delta/backtests", headers=headers, json=BODY).json()["id"]
    drop = client.post("/api/v1/crypto-delta/backtests", headers=headers, json={**BODY, "slippage_bps": "2"}).json()["id"]
    assert client.delete(f"/api/v1/crypto-delta/backtests/{drop}").status_code in (400, 403)          # CSRF is required
    assert client.delete(f"/api/v1/crypto-delta/backtests/{drop}", headers=headers).status_code == 204
    assert [r["id"] for r in client.get("/api/v1/crypto-delta/backtests").json()] == [keep]
    assert client.get(f"/api/v1/crypto-delta/backtests/{drop}").status_code == 404
    assert client.delete(f"/api/v1/crypto-delta/backtests/{drop}", headers=headers).status_code == 404   # already gone
    client.post("/api/v1/auth/logout", headers=headers)
    other = login(client, engine, "intruder@example.com")
    assert client.delete(f"/api/v1/crypto-delta/backtests/{keep}", headers=other).status_code == 404    # someone else's run is untouchable
    with Session(engine) as db:
        assert db.scalar(select(CryptoBacktestRun).where(CryptoBacktestRun.id == UUID(keep))) is not None


def test_an_identical_backtest_submitted_twice_within_a_minute_is_one_run(client, engine, delta_store):
    headers = login(client, engine, "dedupe@example.com")
    seed_candles(engine)
    first = client.post("/api/v1/crypto-delta/backtests", headers=headers, json=BODY)
    second = client.post("/api/v1/crypto-delta/backtests", headers=headers, json=BODY)
    third = client.post("/api/v1/crypto-delta/backtests", headers=headers, json=BODY)
    assert first.status_code == second.status_code == third.status_code == 200
    assert first.json()["id"] == second.json()["id"] == third.json()["id"] and second.json()["deduplicated"] is True and "deduplicated" not in first.json()
    assert len(client.get("/api/v1/crypto-delta/backtests").json()) == 1
    other = client.post("/api/v1/crypto-delta/backtests", headers=headers, json={**BODY, "slippage_bps": "5"})
    assert other.json()["id"] != first.json()["id"] and "deduplicated" not in other.json()                       # a different setting is a different run


def test_forward_test_api_is_paper_only_owner_scoped_and_fails_closed(client, engine, monkeypatch):
    from app.crypto_delta import forward as crypto_forward
    monkeypatch.setattr(crypto_forward, "fetch_spec", lambda symbol, **_: crypto_forward.Spec(symbol, D("0.001"), D("0.0005"), D("0.5")))
    monkeypatch.setattr(crypto_forward, "sync_candles", lambda *a, **k: {})      # no network in tests; candles below are TEST DATA
    headers = login(client, engine, "fwd@example.com")
    seed_candles(engine)
    created = client.post("/api/v1/crypto-delta/forward-tests", headers=headers, json={"symbol": "testusd", "resolution": "15m"})
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["paper_only"] is True and "not investment advice" in body["label"] and body["symbol"] == "TESTUSD"
    assert body["summary"]["closed_trades"] == 0 and body["summary"]["win_rate"] is None       # starts now: no trade before the start
    assert body["params"]["ema"] == 5 and body["params"]["direction"] == -1
    ft_id = body["id"]
    assert client.post(f"/api/v1/crypto-delta/forward-tests/{ft_id}/pause", headers=headers).json()["status"] == "paused"
    assert client.post(f"/api/v1/crypto-delta/forward-tests/{ft_id}/refresh", headers=headers).status_code == 200
    other = login(client, engine, "fwd2@example.com")
    assert client.get(f"/api/v1/crypto-delta/forward-tests/{ft_id}").status_code == 404
    assert client.delete(f"/api/v1/crypto-delta/forward-tests/{ft_id}", headers=other).status_code == 404
    monkeypatch.setattr(crypto_forward, "fetch_spec", lambda symbol, **_: (_ for _ in ()).throw(CandleDataError("Contract value or fee rate is unavailable; refusing to assume it.")))
    assert client.post("/api/v1/crypto-delta/forward-tests", headers=other, json={"symbol": "NOFEE"}).status_code == 422
    assert client.post("/api/v1/crypto-delta/forward-tests", headers=other, json={"symbol": "BTCUSD", "contracts": 0}).status_code == 422


# ---- Delta live execution (mocked broker only; no request leaves the process) ----
def _live_fixture(engine, monkeypatch, armed: bool):
    from pydantic import SecretStr
    from app.crypto_delta.forward import Spec
    from app.delta_execution import policy, service as live_service
    from app.models.crypto_delta import CryptoForwardTest
    monkeypatch.setattr(settings, "delta_live_execution_enabled", True)
    monkeypatch.setattr(settings, "delta_api_key", SecretStr("k"))
    monkeypatch.setattr(settings, "delta_api_secret", SecretStr("s"))
    monkeypatch.setattr(policy, "environment_block", lambda: None)
    with Session(engine) as db:
        user = db.scalar(select(User))
        ft = CryptoForwardTest(user_id=user.id, name="t", symbol="BTCUSD", resolution="15m", params={"ema": 5, "rr": "3", "direction": -1}, contracts=1,
                               slippage_bps=D("5"), start_time=T0, status="active", execute_live=True, created_at=T0, updated_at=T0)
        db.add(ft)
        live_service.get_state(db, user.id).armed = armed
        db.commit()
        return user.id, ft.id, Spec("BTCUSD", D("0.001"), D("0.0005"), D("0.5"), 27, D("0.4"), D("0.2"))


def _entry_and_candles(now):
    close = now.replace(second=0, microsecond=0) - timedelta(minutes=now.minute % 15)
    last = Candle(close - timedelta(minutes=15), D("100000"), D("100100"), D("99800"), D("100000"), D("1"))
    return [{"side": "sell", "entry_time": last.open_time, "signal_time": last.open_time - timedelta(minutes=15), "entry_raw": D("100050"), "stop": D("100400"), "closed_same_bar": False}], [last], close + timedelta(seconds=20)


def test_live_order_is_placed_only_when_every_layer_allows_and_is_idempotent(client, engine, monkeypatch):
    from app.delta_execution import client as dclient, service as live_service
    from app.models.crypto_delta import CryptoForwardTest, DeltaLiveOrder
    login(client, engine, "live@example.com")
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json={"success": True, "result": {"id": 555}})
    factory = lambda: dclient.DeltaOrderClient(transport=httpx.MockTransport(handler))  # noqa: E731
    now = datetime.now(timezone.utc)
    entries, candles, at = _entry_and_candles(now)
    # not armed: blocked, nothing sent
    user_id, ft_id, spec = _live_fixture(engine, monkeypatch, armed=False)
    with Session(engine) as db:
        ft = db.get(CryptoForwardTest, ft_id)
        row = live_service.maybe_place(db, ft, spec, entries, candles, at, factory)
        assert row.status == "blocked" and "armed" in row.block_reason and calls == []
    # armed: leverage set, then one bracket order of one contract
    with Session(engine) as db:
        live_service.get_state(db, user_id).armed = True
        db.query(DeltaLiveOrder).delete()
        db.commit()
        ft = db.get(CryptoForwardTest, ft_id)
        row = live_service.maybe_place(db, ft, spec, entries, candles, at, factory)
        assert row.status == "placed" and row.delta_order_id == "555" and row.contracts == 1 and row.leverage == 25
        assert calls == ["/v2/products/27/orders/leverage", "/v2/orders"]
        again = live_service.maybe_place(db, ft, spec, entries, candles, at, factory)             # the same signal can never place twice
        assert again.id == row.id and calls.count("/v2/orders") == 1
        # a paper-only test never reaches the broker
        ft.execute_live = False
        db.commit()                                                  # the switch is read back from the database when sending
        assert live_service.maybe_place(db, ft, spec, entries, candles, at, factory) is None


def test_arming_needs_2fa_the_flag_and_the_phrase_and_the_kill_switch_always_works(client, engine, monkeypatch):
    from app.delta_execution import policy
    headers = login(client, engine, "arm@example.com")
    body = {"acknowledged": True}
    assert client.post("/api/v1/crypto-delta/live/arm", headers=headers, json=body).status_code == 403          # no 2FA-verified session
    status = client.get("/api/v1/crypto-delta/live/status").json()
    assert status["armed"] is False and status["can_arm"] is False and status["limits"]["max_contracts"] == 1
    assert client.post("/api/v1/crypto-delta/live/disarm", headers=headers).json()["armed"] is False          # stopping never needs 2FA
    assert client.put("/api/v1/crypto-delta/live/tests/00000000-0000-0000-0000-000000000000", headers=headers, json={"enabled": True}).status_code == 404


def test_public_feed_can_be_paused_and_resumed_and_the_choice_is_stored(client, engine, monkeypatch):
    from app.delta_research import collector
    monkeypatch.setattr(collector, "feed_wanted", collector.feed_wanted)
    headers = login(client, engine, "feed@example.com")
    from app.api import crypto_delta as api
    stored = {"on": True}
    monkeypatch.setattr(api, "feed_wanted", lambda: stored["on"])
    monkeypatch.setattr(api, "set_feed_wanted", lambda on: stored.update(on=on))
    assert client.get("/api/v1/crypto-delta/feed").json()["wanted"] is True
    assert client.put("/api/v1/crypto-delta/feed", json={"enabled": False}).status_code in (401, 403)            # needs the CSRF header
    off = client.put("/api/v1/crypto-delta/feed", headers=headers, json={"enabled": False}).json()
    assert off["wanted"] is False and stored["on"] is False
    assert client.put("/api/v1/crypto-delta/feed", headers=headers, json={"enabled": True}).json()["wanted"] is True


def test_the_feed_preference_round_trips_through_the_database(engine, monkeypatch):
    from app.delta_research import collector
    from app import db as app_db
    monkeypatch.setattr(app_db, "engine", engine)
    assert collector.feed_wanted() is True                      # no choice stored: on
    collector.set_feed_wanted(False)
    assert collector.feed_wanted() is False
    collector.set_feed_wanted(True)
    assert collector.feed_wanted() is True


def test_watch_rows_carry_ticker_columns_and_unknown_stays_null(tmp_path):
    with DeltaResearchStore(tmp_path / "w.sqlite3") as store:
        store.save_products([{"id": 1, "symbol": "AAAUSD", "contract_type": "perpetual_futures", "state": "live"}, {"id": 2, "symbol": "BBBUSD", "contract_type": "perpetual_futures", "state": "live"}])
        store.save_quotes([{"product_id": 1, "symbol": "AAAUSD", "close": "10", "mark_price": "10.1", "turnover_usd": "5000", "open": "9", "high": "11", "low": "8", "ltp_change_24h": "11.1",
                            "funding_rate": "0.01", "oi_value_usd": "123456", "quotes": {"best_bid": "9.9", "best_ask": "10.1"}},
                           {"product_id": 2, "symbol": "BBBUSD", "close": "3"}], datetime(2026, 11, 23, tzinfo=timezone.utc), "delta_rest_ticker")
        items = {i["symbol"]: i for i in store.list_products("", 10, 0)["items"]}
    a, b = items["AAAUSD"]["ticker"], items["BBBUSD"]["ticker"]
    assert a["ltp_change_24h"] == "11.1" and a["funding_rate"] == "0.01" and a["oi_value_usd"] == "123456" and a["best_bid"] == "9.9"
    assert b["funding_rate"] is None and b["oi_value_usd"] is None and b["best_bid"] is None            # unknown is null, never zero
