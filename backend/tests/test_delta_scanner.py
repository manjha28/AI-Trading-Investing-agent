"""Multi-perpetual paper scanner. Trades, tickers and candles below are TEST DATA."""
import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest
from sqlalchemy.orm import Session

from app.delta_scanner import service, universe
from app.delta_scanner.portfolio import RiskRules, simulate
from app.models import User
from app.models.delta_scanner import DeltaScanner

T0 = datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc)


def trade(sym, start_min, dur_min, entry="100", stop="99", net="1.5", side="sell"):
    a, b = T0 + timedelta(minutes=start_min), T0 + timedelta(minutes=start_min + dur_min)
    return {"symbol": sym, "side": side, "signal_time": a.isoformat(), "entry_time": a.isoformat(), "exit_time": b.isoformat(), "entry_fill": entry, "stop_initial": stop, "net_pnl": net}


def sim(per, **kw):
    return simulate(per, RiskRules(**{"wallet": D(1000), **kw}))


def test_size_comes_from_half_a_percent_of_the_wallet_and_the_5x_cap():
    # risk 0.5% of 1000 = 5 USD; stop distance 1 USD x contract value 0.1 = 0.1 USD per contract -> 50 contracts, but 5x cap: 5000 / (100*0.1) = 500 -> not binding
    out = sim({"A": {"contract_value": D("0.1"), "trades": [trade("A", 0, 30)]}})
    assert out["trades"][0]["contracts"] == 50 and out["trades"][0]["net_pnl"] == "75.0" and out["total_net_pnl"] == "75.0"
    # a very tight stop would want more contracts than 5x allows: capped and flagged
    tight = sim({"A": {"contract_value": D("1"), "trades": [trade("A", 0, 30, entry="100", stop="99.99")]}})
    assert tight["trades"][0]["contracts"] == 50 and tight["trades"][0]["leverage_capped"] is True       # 5000 / 100 = 50 contracts
    none = sim({"A": {"contract_value": D("1"), "trades": [trade("A", 0, 30, entry="100", stop="40")]}})  # 60 USD per contract > 5 USD budget
    assert none["trades"] == [] and "below one contract" in none["signal_log"][0]["reason"]


def test_total_open_risk_cap_is_two_percent_and_frees_up_when_a_trade_closes():
    per = {s: {"contract_value": D("0.1"), "trades": [trade(s, 0, 60)]} for s in "ABCDE"}          # five simultaneous signals, 5 USD risk each; cap 20 USD
    out = sim(per)
    assert out["signals_taken"] == 4 and sum("total risk cap" in e["reason"] for e in out["signal_log"]) == 1
    later = {**per, "F": {"contract_value": D("0.1"), "trades": [trade("F", 61, 10)]}}               # after the others closed
    assert sim(later)["signals_taken"] == 5


def test_one_position_per_contract_and_signals_are_one_time_ordered_queue():
    per = {"A": {"contract_value": D("0.1"), "trades": [trade("A", 0, 60), trade("A", 10, 20)]}, "B": {"contract_value": D("0.1"), "trades": [trade("B", 5, 20)]}}
    out = sim(per)
    assert [(e["symbol"], e["taken"]) for e in out["signal_log"]] == [("A", True), ("B", True), ("A", False)]
    assert out["per_symbol"] == {"A": {"trades": 1, "net_pnl": "75.0"}, "B": {"trades": 1, "net_pnl": "75.0"}} and out["paper_only"] is True
    assert "balance in use" in out["live_note"]


def test_batch_parsing_and_top_liquid_by_turnover_with_tight_spread():
    assert universe.parse_symbols("btcusd, ETHUSD\nsolusd btcusd") == ["BTCUSD", "ETHUSD", "SOLUSD"]
    with pytest.raises(ValueError):
        universe.parse_symbols("BTCUSD, BTCINR")
    q = lambda b, a: {"best_bid": b, "best_ask": a}  # noqa: E731
    tickers = [{"symbol": "AAAUSD", "turnover_usd": "500", "quotes": q("100", "100.01"), "funding_rate": "0.01"}, {"symbol": "BBBUSD", "turnover_usd": "900", "quotes": q("100", "100.02")},
               {"symbol": "WIDEUSD", "turnover_usd": "9999", "quotes": q("100", "101")}, {"symbol": "NOQUOTEUSD", "turnover_usd": "800"}, {"symbol": "XYZINR", "turnover_usd": "7000", "quotes": q("1", "1.0001")}]
    top = universe.top_liquid(tickers, 2)
    assert [r["symbol"] for r in top] == ["BBBUSD", "AAAUSD"] and top[1]["funding_rate"] == "0.01" and D(top[0]["spread_bps"]) == D("2.00")


def test_a_scanner_run_never_reaches_any_order_adapter(engine, monkeypatch):
    from app.crypto_delta.candles import Candle
    from app.crypto_delta.forward import Spec
    from app.delta_execution import client as dclient, service as live

    def boom(*a, **k):
        raise AssertionError("a paper scanner must never reach an order path")
    monkeypatch.setattr(live, "maybe_place", boom)
    monkeypatch.setattr(live, "attempt_order", boom)
    monkeypatch.setattr(dclient.DeltaOrderClient, "__init__", boom)
    monkeypatch.setattr(dclient.DeltaOrderClient, "place_bracket", boom)
    spec = Spec("AAAUSD", D("0.1"), D("0.0005"), D("0.1"))
    rnd = random.Random(7)
    price, candles = D(100), []
    for i in range(400):
        o = price
        price = max(D(1), price + D(str(round(rnd.uniform(-1.5, 1.5), 2))))
        candles.append(Candle(T0 - timedelta(minutes=15 * (400 - i)), o, max(o, price) + D("0.3"), min(o, price) - D("0.3"), price, D(10)))
    monkeypatch.setattr(service, "fetch_spec", lambda s, transport=None: spec)
    monkeypatch.setattr(service, "sync_candles", lambda *a, **k: {})
    monkeypatch.setattr(service, "load_candles", lambda *a, **k: candles)
    with Session(engine) as db:
        user = User(email="scan@example.com", password_hash="x", email_verified=True, created_at=T0)
        db.add(user); db.flush()
        sc = DeltaScanner(user_id=user.id, name="t", strategy_key="five_ema_alert", resolution="15m", symbols=["AAAUSD", "BBBUSD"], params={"ema": 5, "rr": "3", "direction": 0, "stop_buffer_pct": "0", "slippage_bps": "5"},
                          wallet_usd=D(1000), risk_pct=D("0.005"), total_risk_cap_pct=D("0.02"), leverage_cap=D(5), label="paper", start_time=T0 - timedelta(days=6), status="active", created_at=T0, updated_at=T0)
        db.add(sc); db.commit()
        snap = service.run_once(db, sc, T0)
        assert snap["paper_only"] is True and snap["signals_seen"] >= 1 and snap["symbols_ok"] == 2
        assert not hasattr(DeltaScanner, "execute_live")                                     # structurally paper only
        assert service.run_pass(db, T0 + timedelta(minutes=16)) == {"ran": 1}



def test_funding_extreme_candidate_runs_on_hourly_bars_and_is_labelled_unproven(engine, monkeypatch):
    import pandas as pd

    from app.crypto_delta.candles import Candle
    from app.crypto_delta.forward import Spec
    from app.delta_lab import data
    now = T0
    rnd = random.Random(3)
    n = 24 * 100
    price, candles = D(100), []
    for i in range(n):                                                                                 # TEST DATA: a drifting, noisy series
        o = price
        price = max(D(1), price + D(str(round(rnd.uniform(-0.6, 0.55), 3))))
        candles.append(Candle(now - timedelta(hours=n - i), o, max(o, price) + D("0.2"), min(o, price) - D("0.2"), price, D(10)))
    base = int((now - timedelta(days=100)).timestamp()) // 3600 * 3600
    ft = [base + 3600 * k for k in range(24 * 100)]
    block = {}
    for t in ft:
        block.setdefault(t // 28800, 0.01 if rnd.random() < 0.15 else 0.002 + 0.001 * rnd.random())          # one value per 8 h, as Delta publishes it (percent)
    fund = pd.DataFrame({"time": ft, "open": 0.0, "high": 0.0, "low": 0.0, "close": [block[t // 28800] for t in ft], "volume": None})
    monkeypatch.setattr(service, "sync_candles", lambda *a, **k: {})
    monkeypatch.setattr(service, "load_candles", lambda *a, **k: candles)
    monkeypatch.setattr(data, "fetch_candles", lambda *a, **k: fund)
    spec = Spec("AAAUSD", D("0.1"), D("0.0005"), D("0.1"))
    trades = service._funding_trades(None, "AAAUSD", spec, now - timedelta(days=60), now, None)
    assert len(trades) >= 1 and set(trades[0]) >= {"entry_time", "exit_time", "entry_fill", "stop_initial", "net_pnl", "side"}
    assert all(datetime.fromisoformat(t["entry_time"]) >= now - timedelta(days=60) for t in trades)    # nothing before the start counts
    out = simulate({"AAAUSD": {"contract_value": spec.contract_value, "trades": trades}}, RiskRules())
    assert out["paper_only"] is True and out["signals_seen"] == len(trades)


from tests.test_crypto_delta import engine  # noqa: E402,F401
