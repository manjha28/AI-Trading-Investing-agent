"""NIFTY options buying: underlying signal, contract selection, option execution, look-ahead, missing data, integration.
All market data below is synthetic and exists only to test logic; it is never market evidence."""
import copy
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Instrument, MarketBar
from app.services import intraday_options as iopt
from app.services.intraday_strategy import EvalResult, OptionsUnderlying, StrategyDefinition, evaluate, validate_definition
from app.services.option_selector import SelectionConfig, calculated_delta, select_contract
from app.services.options_data import Candle, Contract, InMemoryOptionData, Quote, coverage_report
from app.services.options_strategy import build_underlying_rules, starter_definition
from app.services.timeframes import EQUITY_IN
from app.services.transaction_costs import compute_charges, get_schedule, trade_executions
from tests.test_signals_lab_api import API, add_inst, client, engine, h, login, post  # noqa: F401  (fixtures)

UTC = timezone.utc
T0 = datetime(2025, 1, 7, 4, 46, tzinfo=UTC)          # 10:16 IST
E1, E2 = datetime(2025, 1, 9, 10, 0, tzinfo=UTC), datetime(2025, 1, 16, 10, 0, tzinfo=UTC)
MASTER = datetime(2025, 1, 1, tzinfo=UTC)


def mkc(strike, typ, expiry=E1, lot=25, valid_from=MASTER, valid_to=None, master="master-2025-01"):
    return Contract(f"{typ}{strike}{expiry:%m%d}", "NIFTY", f"NIFTY {expiry:%d%b} {strike} {typ}".upper(), expiry, D(strike), typ, lot, valid_from, valid_to, master, "test")


def mkq(c, t, bid, ask, delta=None, bq=500, aq=500, vol=1000, iv=None, up=None, src="provider"):
    return Quote(c.id, t, None if bid is None else D(str(bid)), None if ask is None else D(str(ask)), bq, aq, None, vol, None if iv is None else D(str(iv)),
                 None if delta is None else D(str(delta)), (src if delta is not None else None), None if up is None else D(str(up)), "test")


def chain(strikes=(22900, 23000, 23100), expiries=(E1, E2), lot=25):      # 100-point spacing: nothing assumes NIFTY's real interval
    return [mkc(k, t, e, lot) for e in expiries for k in strikes for t in ("CE", "PE")]


def world(deltas=None, t=T0 - timedelta(seconds=2), bid=100.0, ask=100.5, **kw):
    cs = chain()
    deltas = deltas or {}
    qs = [mkq(c, t, bid, ask, deltas.get((c.strike, c.option_type, c.expiry), {"CE": 0.52, "PE": -0.52}[c.option_type]), **kw) for c in cs]
    return InMemoryOptionData(cs, qs)


CFG = SelectionConfig()


def sel(src, direction="long", ref=23020, ts=T0, cfg=CFG):
    return select_contract(src, cfg, underlying="NIFTY", direction=direction, signal_time=ts, reference_price=D(ref))


# ───────────── option selector ─────────────
def test_atm_ce_selection_uses_the_strike_nearest_the_reference_and_logs_why():
    s = sel(world())
    assert s.ok and s.contract.strike == D(23000) and s.contract.option_type == "CE" and s.expiry_date == "2025-01-09"
    assert "closest to preferred 0.55" in s.why and "nearest eligible expiry" in s.why and "spot" in s.why and s.master_version == "master-2025-01"
    assert [c["strike_choice"] for c in s.candidates] == ["atm", "itm1"]


def test_one_strike_itm_is_a_lower_strike_for_calls_and_a_higher_strike_for_puts():
    ce = world({(D(22900), "CE", E1): 0.55, (D(23000), "CE", E1): 0.40})
    s = sel(ce, "long")
    assert s.contract.strike == D(22900) and s.delta == pytest.approx(0.55)                 # ITM call: 22,900 < ATM 23,000
    pe = world({(D(23100), "PE", E1): -0.55, (D(23000), "PE", E1): -0.40})
    s = sel(pe, "short")
    assert s.contract.strike == D(23100) and s.contract.option_type == "PE"               # ITM put: 23,100 > ATM 23,000


def test_pe_is_chosen_for_bearish_signals_and_direction_flags_disable_sides():
    assert sel(world(), "short").contract.option_type == "PE"
    off = sel(world(), "short", cfg=SelectionConfig(allow_pe=False))
    assert not off.ok and off.no_trade_reasons == ["PE_disabled"]


def test_delta_filter_and_preferred_delta_tie_break():
    assert sel(world({(D(23000), "CE", E1): 0.70, (D(22900), "CE", E1): 0.30})).no_trade_reasons[0].startswith("delta_")
    s = sel(world({(D(23000), "CE", E1): 0.47, (D(22900), "CE", E1): 0.58}))
    assert s.contract.strike == D(22900)                                                    # |0.58-0.55| < |0.47-0.55|
    s = sel(world({(D(23000), "CE", E1): 0.56, (D(22900), "CE", E1): 0.54}))
    assert s.contract.strike == D(23000) and "closest" in s.why                             # 0.01 vs 0.01: tie -> nearer the money
    assert sel(world(), cfg=SelectionConfig(delta_min=0.30, delta_max=0.70, delta_preferred=0.31, strike_choices=("atm", "itm1"))).ok


def test_expiry_rank_and_same_day_expiry_exclusion():
    on_expiry_day = datetime(2025, 1, 9, 5, 0, tzinfo=UTC)
    src = world(t=on_expiry_day - timedelta(seconds=2))
    s = sel(src, ts=on_expiry_day)
    assert s.ok and s.expiry_date == "2025-01-16"                                          # E1 expires today: skipped, next expiry used
    only_today = InMemoryOptionData([c for c in chain() if c.expiry == E1], [mkq(c, on_expiry_day - timedelta(seconds=2), 100, 100.5, 0.52) for c in chain() if c.expiry == E1 and c.option_type == "CE"])
    s = sel(only_today, ts=on_expiry_day)
    assert not s.ok and s.no_trade_reasons == ["only_same_day_expiry_available"]
    assert sel(only_today, ts=on_expiry_day, cfg=SelectionConfig(allow_expiry_day=True)).ok  # retained as a separate configurable variant
    assert sel(world(), cfg=SelectionConfig(expiry_rank=1)).expiry_date == "2025-01-16"
    assert sel(world(), cfg=SelectionConfig(expiry_rank=5)).no_trade_reasons == ["no_eligible_expiry"]


@pytest.mark.parametrize("kwargs,reason", [
    (dict(bid=100.0, ask=101.5), "spread_"),                         # 1.49% of midpoint > 0.75
    (dict(bid=None, ask=100.5), "bid_or_ask_missing_or_crossed"),
    (dict(bid=100.5, ask=100.0), "bid_or_ask_missing_or_crossed"),
    (dict(bq=10, aq=500), "insufficient_depth_"),
    (dict(bq=None, aq=None), "depth_unavailable"),
])
def test_quote_quality_rejections_return_no_trade_with_a_reason(kwargs, reason):
    s = sel(world(**kwargs))
    assert not s.ok and s.contract is None and any(r.startswith(reason) for r in s.no_trade_reasons) and s.why.startswith("NO TRADE")


def test_stale_quote_illiquid_and_missing_quote_are_rejected():
    assert any(r.startswith("stale_quote") for r in sel(world(t=T0 - timedelta(seconds=120))).no_trade_reasons)
    assert "no_quote_at_timestamp" in sel(InMemoryOptionData(chain(), [])).no_trade_reasons
    assert "illiquid_volume_missing_or_below_minimum" in sel(world(vol=5), cfg=SelectionConfig(min_volume=100)).no_trade_reasons
    assert sel(world(), cfg=SelectionConfig(quote_max_age_seconds=1)).ok is False         # freshness is strictly configurable


def test_missing_contract_master_lot_size_and_reference_price_mean_no_trade():
    assert sel(InMemoryOptionData([], [])).no_trade_reasons == ["no_contract_master_for_timestamp"]
    nolot = InMemoryOptionData([mkc(23000, "CE", E1, lot=None)], [mkq(mkc(23000, "CE", E1, lot=None), T0 - timedelta(seconds=2), 100, 100.5, 0.5)])
    assert "contract_identity_or_lot_size_unverified" in sel(nolot).no_trade_reasons
    assert select_contract(world(), CFG, underlying="NIFTY", direction="long", signal_time=T0, reference_price=None).no_trade_reasons == ["no_reference_price_for_atm"]


def test_historical_selection_uses_only_information_available_at_the_signal_time():
    late_listing = mkc(23000, "CE", E1, valid_from=T0 + timedelta(minutes=1))            # not yet listed at the signal time
    early = [c for c in chain() if not (c.strike == D(23000) and c.option_type == "CE" and c.expiry == E1)] + [late_listing]
    qs = [mkq(c, T0 - timedelta(seconds=2), 100, 100.5, 0.52) for c in early]
    s = sel(InMemoryOptionData(early, qs))
    assert s.contract.strike != D(23000) or s.contract.valid_from <= T0
    future_quote = world()
    future_quote.quotes.append(mkq(future_quote.contracts[0], T0 + timedelta(seconds=5), 1, 400, 0.99))    # a LATER quote must be invisible
    assert sel(future_quote).ok and sel(future_quote).delta == pytest.approx(0.52)
    expired = InMemoryOptionData([mkc(23000, "CE", datetime(2025, 1, 7, 4, 0, tzinfo=UTC))], [])
    assert sel(expired).no_trade_reasons == ["no_contract_master_for_timestamp"]           # contract already expired at T0
    superseded = [mkc(23000, "CE", E1, lot=25, valid_to=T0 - timedelta(hours=1), master="m-old"), mkc(23000, "CE", E1, lot=75, valid_from=T0 - timedelta(hours=1), master="m-new")]
    s = sel(InMemoryOptionData(superseded, [mkq(c, T0 - timedelta(seconds=2), 100, 100.5, 0.52, bq=900, aq=900) for c in superseded]), cfg=SelectionConfig(strike_choices=("atm",)))
    assert s.contract.lot_size == 75 and s.master_version == "m-new"                       # effective-dated lot size, never hardcoded


def test_greeks_are_provider_or_calculated_from_contemporaneous_iv_and_never_invented():
    base = world()
    c = next(x for x in base.contracts if x.strike == D(23000) and x.option_type == "CE" and x.expiry == E1)
    no_greek = InMemoryOptionData(base.contracts, [mkq(x, T0 - timedelta(seconds=2), 100, 100.5, None) for x in base.contracts])
    s = sel(no_greek)
    assert not s.ok and "delta_unavailable" in s.no_trade_reasons                           # no delta, no IV: refuse
    with_iv = InMemoryOptionData(base.contracts, [mkq(x, T0 - timedelta(seconds=2), 100, 100.5, None, iv=14.0, up=23020) for x in base.contracts])
    s = sel(with_iv)
    assert s.ok and s.delta_source == "calculated" and 0.45 <= abs(s.delta) <= 0.60
    assert calculated_delta("CE", 23020, 23000, E1, T0, 14.0, 6.5) == pytest.approx(s.delta if s.contract.strike == c.strike else s.delta, abs=0.2)
    assert calculated_delta("CE", 23000, 23000, E1, T0, None, 6.5) is None and calculated_delta("CE", 23000, 23000, E1, T0, 0, 6.5) is None
    assert sel(with_iv, cfg=SelectionConfig(allow_calculated_greeks=False)).no_trade_reasons.count("delta_unavailable") >= 1
    assert calculated_delta("PE", 23000, 23000, E1, T0, 14.0, 6.5) < 0


# ───────────── underlying signal ─────────────
def mk_market(seed, days=12, drift=0.02, vol=0.5):
    rng = np.random.default_rng(seed)
    frames, price = [], 22000.0
    for k in range(days):
        day = pd.Timestamp("2025-01-06") + pd.Timedelta(days=k)
        if day.dayofweek >= 5:
            continue
        r = rng.normal(drift * (1 if k % 3 else -1), vol, 375) + np.sin(np.arange(375) / 15) * 0.4
        c = price + np.cumsum(r)
        price = c[-1]
        o = np.r_[c[0], c[:-1]]
        v = rng.integers(1000, 3000, 375).astype(float)
        v[rng.random(375) < 0.15] *= 3
        frames.append(pd.DataFrame({"time": pd.date_range(f"{day.date()} 03:45", periods=375, freq="min", tz="UTC"), "open": o, "high": np.maximum(o, c) + abs(rng.normal(0, 0.2, 375)),
                                    "low": np.minimum(o, c) - abs(rng.normal(0, 0.2, 375)), "close": c, "volume": v}))
    return pd.concat(frames, ignore_index=True)


def make_def(**u):
    d = starter_definition()
    if u:
        d["options"]["underlying"].update(u)
        d.update(build_underlying_rules(OptionsUnderlying.model_validate(d["options"]["underlying"])))
    return StrategyDefinition.model_validate(d)


def test_compiled_rules_follow_the_specification():
    d = starter_definition()
    ind = {i["alias"]: i for i in d["indicators"]}
    assert ind["t_vwap"]["timeframe"] == ind["t_fast"]["timeframe"] == ind["t_adx"]["timeframe"] == "5m" and ind["e_rsi"]["timeframe"] == "1m"
    assert (ind["t_fast"]["params"]["length"], ind["t_slow"]["params"]["length"], ind["t_adx"]["params"]["length"], ind["e_alt"]["params"]["length"]) == (9, 21, 14, 20)
    text = str(d["long_entry"])
    for needle in ("'t_vwap'", "'t_adx'", "'plus_di'", "'minus_di'", "'time_window'", "'09:30'", "'14:45'", "'e_swing'", "'e_vol'", "1.2", "0.25", "52.0", "1.5"):
        assert needle in text, needle
    # EMA 21 rising across the last THREE completed 5m candles: ema21 > ema21[-1] > ema21[-2]
    slopes = [c for c in d["long_entry"]["children"][0]["children"] if c["type"] == "compare" and c["left"].get("ref") == "t_slow" and c["right"].get("ref") == "t_slow"]
    assert [(c["left"]["offset"], c["right"]["offset"], c["op"]) for c in slopes] == [(0, 1, ">"), (1, 2, ">")]
    short = str(d["short_entry"])
    assert "48.0" in short and "'<'" in short and "'minus_di'" in short
    assert validate_definition(StrategyDefinition.model_validate(d))[1] is not None


def test_long_and_short_signals_exist_and_use_only_completed_candles():
    d = make_def()
    f = mk_market(2)
    r = evaluate(d, f, EQUITY_IN)
    assert r.signals["long_entry"].sum() >= 1 and r.signals["short_entry"].sum() >= 1
    for cut in (1200, 2500, 3400):
        part = evaluate(d, f.iloc[:cut].reset_index(drop=True), EQUITY_IN)
        for rule in ("long_entry", "short_entry"):
            assert np.array_equal(r.signals[rule][:cut], part.signals[rule]), (rule, cut)        # no unfinished 5m candle, no future data
    idx = np.flatnonzero(r.signals["long_entry"] | r.signals["short_entry"])
    local = (f["time"].iloc[idx] + pd.Timedelta(minutes=1)).dt.tz_convert("Asia/Kolkata")
    mod = (local.dt.hour * 60 + local.dt.minute).to_numpy()
    assert ((mod >= 9 * 60 + 30) & (mod <= 14 * 60 + 45)).all()                                    # session window: 09:30-14:45 IST


@pytest.mark.parametrize("change", [dict(adx_min=99.0), dict(volume_multiple=20.0), dict(rsi_long_min=99.0, rsi_short_max=1.0), dict(max_candle_range_atr=0.01), dict(start_time="15:20", last_entry_time="15:29"),
                                    dict(pullback_atr=0.0, pullback_lookback=1, max_vwap_extension_atr=0.0)])
def test_every_filter_can_turn_the_signal_into_no_trade(change):
    base = evaluate(make_def(), mk_market(2), EQUITY_IN)
    assert base.signals["long_entry"].sum() + base.signals["short_entry"].sum() > 0
    r = evaluate(make_def(**change), mk_market(2), EQUITY_IN)
    assert r.signals["long_entry"].sum() + r.signals["short_entry"].sum() < base.signals["long_entry"].sum() + base.signals["short_entry"].sum()
    if "adx_min" in change or "volume_multiple" in change or "start_time" in change:
        assert r.signals["long_entry"].sum() + r.signals["short_entry"].sum() == 0


def test_two_sided_regime_is_mutually_exclusive():
    r = evaluate(make_def(), mk_market(7), EQUITY_IN)
    assert not (r.signals["long_entry"] & r.signals["short_entry"]).any()


def test_options_definition_validation():
    d = starter_definition()
    d["options"]["selection"].update(delta_min=0.7, delta_max=0.5)
    assert validate_definition(StrategyDefinition.model_validate(d))[1] is None
    d = starter_definition(); d["options"]["underlying"]["trend_timeframe"] = "7m"
    assert validate_definition(StrategyDefinition.model_validate(d))[1] is None
    d = starter_definition(); d["decision_timeframe"] = "5m"
    assert validate_definition(StrategyDefinition.model_validate(d))[1] is None            # decision timeframe must equal the entry timeframe
    d = starter_definition(); d["options"]["execution"]["trailing_activation_r"] = 1.0
    assert validate_definition(StrategyDefinition.model_validate(d))[1] is None            # activation needs a distance
    d = starter_definition(); d["options"]["execution"]["price_mode"] = "indicative_ohlc"
    iss, req = validate_definition(StrategyDefinition.model_validate(d))
    assert req is not None and any("INDICATIVE" in i["message"] or "indicative" in i["message"] for i in iss)
    with pytest.raises(Exception):
        StrategyDefinition.model_validate({**starter_definition(), "options": {**starter_definition()["options"], "risk": {"max_positions": 2}}})   # overlapping positions unsupported


# ───────────── option execution ─────────────
def frame_flat(n=120, price=23000.0):
    t = pd.date_range("2025-01-07 03:45", periods=n, freq="min", tz="UTC")
    return pd.DataFrame({"time": t, "open": price, "high": price + 1, "low": price - 1, "close": price, "volume": 1000.0})


def inject(f, **rules):
    sig = {k: np.zeros(len(f), bool) for k in ("long_entry", "short_entry", "long_exit", "short_exit")}
    for k, idx in rules.items():
        for i in (idx if isinstance(idx, (list, tuple)) else [idx]):
            sig[k][i] = True
    return EvalResult(f["time"], np.arange(len(f)), sig, {}, {}, {}, [])


def options_def(**ex):
    d = starter_definition()
    d["options"]["execution"].update(ex)
    return StrategyDefinition.model_validate(d)


SIG_I = 60                                      # signal bar opens 04:45 UTC, closes 04:46 = T0
CE = mkc(23000, "CE")
PE = mkc(23000, "PE")
ZERO_FEE = {k: 0 for k in ("brokerage_flat", "brokerage_pct", "stt_sell_pct", "exchange_txn_pct", "sebi_pct", "stamp_buy_pct", "ipft_pct", "gst_pct")}


def path_source(points, contract=CE, pre=(100.0, 100.5), delta=0.52, extra_contracts=()):
    """points: [(seconds after T0, bid, ask)] ; a pre-signal quote at T0-2s feeds the selector."""
    cs = [mkc(23000, "CE"), mkc(22900, "CE"), mkc(23000, "PE"), mkc(23100, "PE"), *extra_contracts]
    qs = []
    for c in cs:
        qs.append(mkq(c, T0 - timedelta(seconds=2), pre[0], pre[1], delta if c.option_type == "CE" else -delta))
    for sec, bid, ask in points:
        qs.append(mkq(contract, T0 + timedelta(seconds=sec), bid, ask, delta if contract.option_type == "CE" else -delta))
    return InMemoryOptionData(cs, qs)


def go(points, f=None, rules=None, defn=None, capital=1_000_000, **kw):
    f = f if f is not None else frame_flat()
    res = inject(f, **(rules or {"long_entry": SIG_I}))
    return iopt.run_options_backtest(defn or options_def(), f, res, EQUITY_IN, path_source(points, kw.pop("contract", CE)), initial_capital=D(capital), **kw)


def test_buys_at_ask_sells_at_bid_on_the_first_quote_after_the_signal():
    r = go([(0.5, 90, 91), (3, 101.0, 101.4), (20, 100.0, 100.4), (320, 100.2, 100.6)])        # the 0.5s quote is inside the latency window: not executable
    t = r["trades"][0]
    assert D(t["entry_raw"]) == D("101.4") and D(t["option"]["entry_ask"]) == D("101.4") and D(t["option"]["entry_bid"]) == D("101")
    assert t["entry_time"] == (T0 + timedelta(seconds=3)).isoformat()                              # never the pre-signal 100.5 or the 0.5s quote
    assert t["exit_reason"] == "time_exit" and D(t["exit_raw"]) == D("100.2") and D(t["option"]["exit_bid"]) == D("100.2")   # SOLD at the bid, not the ask
    assert t["option"]["price_source"] == "quote" and D(t["gross_pnl"]) == ((D("100.2") - D("101.4")) * int(t["qty"])).quantize(D("0.01"))


def test_premium_stop_target_and_gap_through_stop():
    stop = go([(3, 100.0, 100.4), (30, 90.0, 90.4)])["trades"][0]                              # stop = 100.4 x 0.88 = 88.35 -> tick 88.35; bid 90 above -> no
    assert stop["exit_reason"] != "premium_stop"
    hit = go([(3, 100.0, 100.4), (30, 88.0, 88.4)])["trades"][0]
    assert hit["exit_reason"] == "premium_stop" and D(hit["exit_raw"]) == D("88") and hit["option"]["gap_through_stop"] is True      # bid 88.00 < stop 88.35: filled at the bid
    tgt = go([(3, 100.0, 100.4), (40, 118.5, 118.9)])["trades"][0]                              # target = 100.4 x 1.18 = 118.47
    assert tgt["exit_reason"] == "target" and D(tgt["target"]) == D("118.45") and D(tgt["exit_raw"]) == D("118.5")
    assert float(D(tgt["target"]) / D(tgt["entry_fill"]) - 1) == pytest.approx(0.18, abs=0.001)         # 12% x 1.5R = 18%


def test_trailing_exit_after_activation():
    d = options_def(trailing_activation_r=1.0, trailing_distance_pct=5.0)
    pts = [(3, 100.0, 100.4), (20, 112.0, 112.4), (40, 117.0, 117.4), (60, 110.9, 111.3)]       # activation +12%; high 117 -> trail 111.15; 110.9 <= 111.15
    t = go(pts, defn=d)["trades"][0]
    assert t["exit_reason"] == "trailing_stop" and D(t["exit_raw"]) == D("110.9")


def test_five_minute_time_exit_regardless_of_profit():
    t = go([(3, 100.0, 100.4), (100, 104.0, 104.4), (310, 105.0, 105.4), (400, 106.0, 106.4)])["trades"][0]
    assert t["exit_reason"] == "time_exit" and t["exit_time"] == (T0 + timedelta(seconds=310)).isoformat() and t["holding_minutes"] <= 5.2


def test_underlying_invalidation_exits_before_the_premium_stop():
    f = frame_flat()
    for i in range(0, 59):                                  # build a confirmed swing low (pivot at 40, confirmed by 46) that price then breaks at bar 62
        f.loc[i, ["open", "high", "low", "close"]] = [23010, 23020, 23000, 23010]
    f.loc[40, ["low", "close", "open"]] = [22980, 22995, 23005]
    f.loc[62, ["open", "high", "low", "close"]] = [23000, 23001, 22970, 22975]               # completed 1m close below the swing low 22980
    pts = [(3, 100.0, 100.4), (60, 99.0, 99.4), (170, 98.0, 98.4), (310, 97.0, 97.4)]
    t = go(pts, f=f)["trades"][0]
    assert t["exit_reason"] == "underlying_invalidation" and t["option"]["invalidation_level"] == 22980.0
    assert float(t["exit_raw"]) > 88.35                                                          # premium stop never reached: the underlying level fired first
    off = go(pts, f=f, defn=options_def(underlying_invalidation=False))["trades"][0]
    assert off["exit_reason"] == "time_exit"


def test_opposite_signal_and_thesis_exits():
    pts = [(3, 100.0, 100.4), (70, 101.0, 101.4), (200, 102.0, 102.4), (310, 102.0, 102.4)]
    opp = go(pts, rules={"long_entry": SIG_I, "short_entry": SIG_I + 2})["trades"][0]
    assert opp["exit_reason"] == "opposite_signal"
    th = go(pts, rules={"long_entry": SIG_I, "long_exit": SIG_I + 2})["trades"][0]
    assert th["exit_reason"] == "thesis_invalidated"
    assert go(pts, rules={"long_entry": SIG_I, "short_entry": SIG_I + 2}, defn=options_def(opposite_signal_exit=False))["trades"][0]["exit_reason"] == "time_exit"


def test_slippage_ticks_and_transaction_costs():
    t = go([(3, 100.0, 100.4), (40, 118.5, 118.9)], defn=options_def(slippage_ticks=2))["trades"][0]
    assert D(t["entry_fill"]) == D("100.5") and D(t["exit_fill"]) == D("118.4")                                # +2 ticks (0.05) on the ask, -2 ticks on the bid
    lots, lot = t["option"]["lots"], t["option"]["lot_size"]
    qty = D(lots * lot)
    costs = compute_charges(trade_executions("long", qty, D("100.5"), D("118.4")), get_schedule("dhan_nse_options_2026_11"))
    assert D(t["charges"]) == costs.total and t["cost_breakdown"]["brokerage"] == "40.00" and D(t["net_pnl"]) == ((D("118.4") - D("100.5")) * qty - costs.total).quantize(D("0.01"))
    assert D(t["slippage_cost"]) == (D("0.10") + D("0.10")) * qty and D(t["gross_pnl"]) == (D("118.5") - D("100.4")) * qty
    assert any("UNVERIFIED" in w for w in go([(3, 100.0, 100.4), (40, 118.5, 118.9)])["warnings"])


def test_lot_rounding_risk_budget_and_capital():
    t = go([(3, 100.0, 100.4), (40, 118.5, 118.9)], capital=2_000_000)["trades"][0]
    entry = D("100.4")
    stop = (entry * D("0.88")).quantize(D("0.05"))
    est = compute_charges(trade_executions("long", D(25), entry, stop), get_schedule("dhan_nse_options_2026_11")).total
    per_lot = (entry - stop) * 25 + est
    expect = int((D(2_000_000) * D("0.0025") / per_lot).to_integral_value(rounding="ROUND_FLOOR"))
    assert t["option"]["lots"] == expect and int(t["qty"]) == expect * 25                           # whole lots from the 0.25% risk budget
    small = go([(3, 100.0, 100.4), (40, 118.5, 118.9)], capital=50_000)
    assert small["metrics"]["total_trades"] == 0 and small["options_report"]["signals"]["no_trade_reasons"] == {"single_lot_exceeds_risk_budget": 1}
    fixed = go([(3, 100.0, 100.4), (40, 118.5, 118.9)], defn=StrategyDefinition.model_validate({**starter_definition(), "options": {**starter_definition()["options"], "risk": {"sizing_method": "fixed_lots", "fixed_lots": 3}}}))
    assert fixed["trades"][0]["option"]["lots"] == 3


def test_no_overlapping_positions_and_daily_loss_limit_and_entry_window():
    f = frame_flat()
    two = go([(3, 100.0, 100.4), (100, 100.0, 100.4), (320, 100.0, 100.4)], f=f, rules={"long_entry": [SIG_I, SIG_I + 1]})
    assert two["metrics"]["total_trades"] == 1 and two["options_report"]["signals"]["no_trade_reasons"]["position_already_open"] == 1
    # a stop-out larger than the 0.75% daily budget blocks later entries that day
    losing = go([(3, 100.0, 100.4), (30, 85.0, 85.4)], f=f, capital=2_000_000, rules={"long_entry": [SIG_I]})
    assert losing["trades"][0]["exit_reason"] == "premium_stop"
    late = frame_flat(375)                                                           # bar 355 closes 15:11 IST: after the 14:45 last new entry
    r = iopt.run_options_backtest(options_def(), late, inject(late, long_entry=355), EQUITY_IN, path_source([]), initial_capital=D(1_000_000))
    assert r["metrics"]["total_trades"] == 0 and "outside_entry_window" in r["options_report"]["signals"]["no_trade_reasons"]
    early = frame_flat(375)                                                          # bar 5 closes 09:21 IST: before the 09:30 start
    assert "outside_entry_window" in iopt.run_options_backtest(options_def(), early, inject(early, long_entry=5), EQUITY_IN, path_source([]), initial_capital=D(1_000_000))["options_report"]["signals"]["no_trade_reasons"]


def test_daily_loss_limit_blocks_new_entries_after_losses():
    f = frame_flat()
    d = StrategyDefinition.model_validate({**starter_definition(), "options": {**starter_definition()["options"], "risk": {"risk_per_trade_pct": 0.5, "daily_loss_limit_pct": 0.4}}})
    # second signal 6 minutes later, after the first trade stopped out
    q2 = T0 + timedelta(minutes=6)
    cs = [mkc(23000, "CE"), mkc(22900, "CE")]
    qs = [mkq(c, T0 - timedelta(seconds=2), 100, 100.5, 0.52) for c in cs] + [mkq(CE, T0 + timedelta(seconds=3), 100, 100.4, 0.52), mkq(CE, T0 + timedelta(seconds=30), 80, 80.4, 0.52)]
    qs += [mkq(c, q2 - timedelta(seconds=2), 100, 100.5, 0.52) for c in cs] + [mkq(CE, q2 + timedelta(seconds=3), 100, 100.4, 0.52)]
    r = iopt.run_options_backtest(d, f, inject(f, long_entry=[SIG_I, SIG_I + 6]), EQUITY_IN, InMemoryOptionData(cs, qs), initial_capital=D(1_000_000))
    assert r["metrics"]["total_trades"] == 1 and r["options_report"]["signals"]["no_trade_reasons"].get("daily_loss_limit_reached") == 1


def test_event_blackout_blocks_entries_and_none_is_invented():
    d = starter_definition()
    assert d["options"]["event_blackouts"] == []                                                  # no event calendar is ever fabricated
    d["options"]["event_blackouts"] = [{"start": "2025-01-07T04:30:00+00:00", "end": "2025-01-07T05:00:00+00:00", "label": "RBI policy"}]
    r = go([(3, 100.0, 100.4)], defn=StrategyDefinition.model_validate(d))
    assert r["metrics"]["total_trades"] == 0 and "event_blackout:RBI policy" in r["options_report"]["signals"]["no_trade_reasons"]


def test_indicative_ohlc_mode_is_labelled_conservative_and_stop_wins_a_same_candle_tie():
    d = options_def(price_mode="indicative_ohlc", ohlc_half_spread_pct=0.5)
    cs = [mkc(23000, "CE"), mkc(22900, "CE")]
    qs = [mkq(c, T0 - timedelta(seconds=2), 100, 100.5, 0.52) for c in cs]
    cands = [Candle(T0 + timedelta(minutes=k), D(o), D(h_), D(l), D(c_), D(500)) for k, (o, h_, l, c_) in enumerate([(100, 101, 99, 100.5), (100.5, 125, 85, 100)])]
    src = InMemoryOptionData(cs, qs, {CE.id: cands})
    f = frame_flat()
    r = iopt.run_options_backtest(d, f, inject(f, long_entry=SIG_I), EQUITY_IN, src, initial_capital=D(1_000_000))
    t = r["trades"][0]
    assert t["option"]["price_source"] == "ohlc_indicative" and D(t["entry_raw"]) == D("100.5") and D(t["entry_fill"]) == D("100.5")          # open 100 x (1 + 0.5%)
    assert t["exit_reason"] == "premium_stop" and t["ambiguous_stop_and_target_same_bar"] is True                                 # candle touched both: stop first, recorded
    assert any("INDICATIVE" in w for w in r["warnings"]) and "INDICATIVE" in r["options_report"]["price_basis"]
    assert iopt.run_options_backtest(options_def(price_mode="quote_accurate"), f, inject(f, long_entry=SIG_I), EQUITY_IN, src, initial_capital=D(1_000_000))["metrics"]["total_trades"] == 0   # candles never silently satisfy quote mode


def test_gap_through_stop_in_indicative_mode_fills_at_the_open():
    d = options_def(price_mode="indicative_ohlc", ohlc_half_spread_pct=0.0)
    cs = [mkc(23000, "CE"), mkc(22900, "CE")]
    qs = [mkq(c, T0 - timedelta(seconds=2), 100, 100.5, 0.52) for c in cs]
    cands = [Candle(T0, D(100), D(100.5), D(99.5), D(100), D(500)), Candle(T0 + timedelta(minutes=1), D(80), D(81), D(79), D(80), D(500))]
    f = frame_flat()
    t = iopt.run_options_backtest(d, f, inject(f, long_entry=SIG_I), EQUITY_IN, InMemoryOptionData(cs, qs, {CE.id: cands}), initial_capital=D(1_000_000))["trades"][0]
    assert t["exit_reason"] == "premium_stop" and D(t["exit_raw"]) == D("80") and t["option"]["gap_through_stop"] is True


# ───────────── look-ahead, reproducibility, missing data ─────────────
def test_future_quotes_and_prices_cannot_change_an_earlier_entry_or_exit():
    pts = [(3, 100.0, 100.4), (40, 118.5, 118.9)]
    base = go(pts)["trades"][0]
    more = go(pts + [(400, 1.0, 1.5), (900, 500.0, 501.0)])["trades"][0]                       # later quotes appear
    assert {k: v for k, v in base.items() if k != "explain"} == {k: v for k, v in more.items() if k != "explain"}
    f2 = frame_flat()
    f2.loc[100:, ["open", "high", "low", "close"]] = 1.0                                        # the underlying collapses after the trade ended
    assert go(pts, f=f2)["trades"][0]["exit_time"] == base["exit_time"]


def test_selection_ignores_a_better_contract_that_only_appears_later():
    pts = [(3, 100.0, 100.4), (40, 118.5, 118.9)]
    src = path_source(pts)
    src.quotes.append(mkq(mkc(22900, "CE"), T0 + timedelta(seconds=1), 100, 100.1, 0.55))       # perfect delta/spread, but only AFTER the signal
    f = frame_flat()
    t = iopt.run_options_backtest(options_def(), f, inject(f, long_entry=SIG_I), EQUITY_IN, src, initial_capital=D(1_000_000))["trades"][0]
    assert t["option"]["strike"] == "23000.0" or t["option"]["strike"] == "23000"


def test_runs_are_reproducible_and_record_provenance():
    a, b = go([(3, 100.0, 100.4), (40, 118.5, 118.9)]), go([(3, 100.0, 100.4), (40, 118.5, 118.9)])
    assert a["trades"] == b["trades"] and a["option_dataset"]["fingerprint_sha256"] == b["option_dataset"]["fingerprint_sha256"]
    assert a["option_dataset"]["master_versions"] == ["master-2025-01"] and a["versions"]["options_engine"].startswith("options-engine/")
    t = a["trades"][0]["option"]
    assert t["contract_id"] and t["selection_why"] and t["master_version"] == "master-2025-01" and t["delta_source"] == "provider"
    assert a["assumptions"]["entry"].endswith("ASK") and a["assumptions"]["exit"] == "sold at the BID"
    assert a["options_report"]["price_basis"].startswith("actual recorded option quotes")


def test_no_option_data_means_no_trades_not_substituted_prices():
    f = frame_flat()
    empty = iopt.run_options_backtest(options_def(), f, inject(f, long_entry=SIG_I), EQUITY_IN, InMemoryOptionData([], []), initial_capital=D(1_000_000))
    assert empty["metrics"]["total_trades"] == 0 and empty["options_report"]["signals"]["no_trade_reasons"] == {"no_contract:no_contract_master_for_timestamp": 1}
    no_quotes_after = go([])
    assert no_quotes_after["metrics"]["total_trades"] == 0 and "no_executable_quote_after_signal" in no_quotes_after["options_report"]["signals"]["no_trade_reasons"]


def test_options_report_contents_and_validation_modes():
    r = go([(3, 100.0, 100.4), (40, 118.5, 118.9)])
    rep = r["options_report"]
    for key in ("ce_trades", "pe_trades", "avg_entry_spread_pct", "avg_exit_spread_pct", "avg_delta_at_entry", "avg_mae_pct", "avg_mfe_pct", "by_delta_bucket", "by_expiry", "by_hour_ist", "by_option_type", "by_config", "signals"):
        assert key in rep
    assert rep["ce_trades"] == 1 and rep["pe_trades"] == 0 and rep["avg_delta_at_entry"] == pytest.approx(0.52) and "0.50-0.55" in rep["by_delta_bucket"] and "stop 12% / 1.5R" in rep["by_config"]
    m = r["metrics"]
    assert "net_profit" in m and "total_charges" in m and any("too small a sample" in w for w in r["warnings"]) and m["sharpe_ratio"] is None
    assert r["equity_curve"] and r["trades"][0]["cost_breakdown"]["total"]
    f = mk_market(2)
    d = make_def()
    res = evaluate(d, f, EQUITY_IN)
    src = InMemoryOptionData([], [])
    d2 = StrategyDefinition.model_validate({**d.model_dump(mode="json")})
    for kind in ("is_oos", "sensitivity", "walk_forward"):
        out = iopt.run_validation(kind, d2, f, res, EQUITY_IN, src, initial_capital=D(1_000_000), train_days=4, test_days=2)
        assert out["mode"] in ("in_out_of_sample", "sensitivity", "walk_forward")
    sens = iopt.run_validation("sensitivity", d2, f, res, EQUITY_IN, src, initial_capital=D(1_000_000))
    assert len(sens["grid"]) == 12 and sens["window"] == "in-sample only"                          # stops 8/10/12/15 x targets 1.25/1.5/2


# ───────────── data coverage, API and Strategy Library integration ─────────────
def test_coverage_report_on_an_empty_database_refuses_and_lists_blockers(client, engine):
    with Session(engine) as db:
        cov = coverage_report(db, T0)
    assert cov["backtest_mode"] == "unavailable" and cov["backtest_ready"] is False
    assert all(v["status"] == "missing" for k, v in cov["datasets"].items() if k != "contract_master" or True)
    assert any("futures" in b for b in cov["blockers"]) and any("contract master" in b for b in cov["blockers"]) and any("option quotes" in b or "option premiums" in b for b in cov["blockers"])
    assert "never substituted" in cov["disclaimer"]


def test_coverage_report_distinguishes_candle_only_from_quote_accurate(client, engine):
    from app.models.strategies import OptionContract, OptionQuote
    with Session(engine) as db:
        fut = Instrument(symbol="NIFTY25JANFUT", display_name="NIFTY Jan FUT", asset_class="future", venue="NSE", currency="INR", country="IN", timezone="Asia/Kolkata", multiplier=D(25), expiry=E1.date(), created_at=T0)
        db.add(fut)
        db.flush()
        db.add(MarketBar(instrument_id=fut.id, interval="1m", timestamp=T0, open=D(1), high=D(1), low=D(1), close=D(1), volume=D(1), source="test", retrieved_at=T0, is_synthetic=False))
        oc = OptionContract(underlying="NIFTY", trading_symbol="NIFTY 09JAN2025 23000 CE", expiry=E1, strike=D(23000), option_type="CE", lot_size=25, valid_from=MASTER, source="test", master_version="m1", ingested_at=T0)
        db.add(oc)
        db.flush()
        db.commit()
        cov = coverage_report(db, T0)
        assert cov["datasets"]["futures_1m"]["status"] == "available" and cov["datasets"]["contract_master"]["lot_size_known"] == 1 and cov["backtest_mode"] == "unavailable"
        db.add(OptionQuote(contract_id=oc.id, quote_time=T0, ingested_at=T0, bid=D(100), ask=D("100.5"), bid_qty=500, ask_qty=500, delta=D("0.5"), greeks_source="provider", source="test"))
        db.commit()
        assert coverage_report(db, T0)["backtest_mode"] == "quote_accurate"


def test_starter_is_in_the_library_opens_edits_versions_and_blocks_backtests_without_data(client, engine):
    login(client, engine)
    with Session(engine) as db:
        fi = Instrument(symbol="NIFTY25JANFUT", display_name="NIFTY Jan FUT", asset_class="future", venue="NSE", currency="INR", country="IN", timezone="Asia/Kolkata", multiplier=D(25), expiry=E1.date(), created_at=T0)
        db.add(fi)
        db.commit()
        fut = str(fi.id)
    post(client, "/starters/install", {}, 200)
    rows = client.get(f"{API}/strategies").json()
    opt = next(r for r in rows if r["name"] == "NIFTY Momentum Options Scalper")
    assert len(rows) == 14 and opt["origin"] == "starter" and opt["origin_label"] == "Built-in Starter Strategy" and opt["strategy_type"] == "options_buying" and opt["tested"] is False and opt["last_backtest"] is None
    s = opt["options_summary"]
    assert (s["market"], s["type"], s["directions"], s["base_data"], s["trend_timeframe"], s["max_duration_minutes"]) == ("NIFTY options", "Options buying", ["CE", "PE"], "1-minute", "5m", 5.0)
    assert opt["data_coverage"]["mode"] == "unavailable" and opt["data_coverage"]["ready"] is False and opt["data_coverage"]["blockers"]
    detail = client.get(f"{API}/strategies/{opt['id']}").json()
    d = copy.deepcopy(detail["versions"][0]["definition"])
    assert d["options"]["execution"]["premium_stop_pct"] == 12.0 and d["options"]["selection"]["delta_preferred"] == 0.55 and d["options"]["risk"]["risk_per_trade_pct"] == 0.25
    # customize EVERY group, save as a new strategy (original untouched) and as a new version
    c = copy.deepcopy(d)
    c["name"], c["universe"] = "My NIFTY scalper", [fut]
    c["source"] = {"type": "visual", "artifact": None, "provenance": {"derived_from": {"strategy_id": opt["id"], "version": 1, "starter_key": "nifty_options_scalper", "name": opt["name"]}}}
    c["options"]["underlying"].update(adx_min=22.0, ema_fast=8, rsi_long_min=55.0, volume_multiple=1.5)
    c["options"]["selection"].update(delta_min=0.40, delta_max=0.65, delta_preferred=0.5, max_spread_pct=0.5, quote_max_age_seconds=10, allow_expiry_day=True)
    c["options"]["execution"].update(premium_stop_pct=10.0, target_r=2.0, trailing_activation_r=1.0, trailing_distance_pct=5.0, max_hold_minutes=4.0, slippage_ticks=1)
    c["options"]["risk"].update(risk_per_trade_pct=0.5, daily_loss_limit_pct=1.0)
    comp = post(client, "/options/compile", c["options"]["underlying"], 200).json()
    c.update(comp)
    assert "22.0" in str(c["long_entry"]) and any(i["params"].get("length") == 8 for i in c["indicators"])
    mine = post(client, "/strategies", {"definition": c}, 201).json()
    assert mine["origin"] == "customized" and mine["strategy_type"] == "options_buying"
    assert client.get(f"{API}/strategies/{opt['id']}").json()["versions"][0]["definition"] == d
    c2 = copy.deepcopy(c)
    c2["options"]["execution"]["premium_stop_pct"] = 8.0
    v2 = post(client, f"/strategies/{mine['id']}/versions", {"definition": c2}, 201).json()
    assert v2["current_version"] == 2 and [v["definition"]["options"]["execution"]["premium_stop_pct"] for v in v2["versions"]] == [8.0, 10.0]      # v1 immutable
    r = post(client, "/backtests", {"strategy_id": mine["id"], "instruments": [fut]})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "options_data_unavailable" and r.json()["detail"]["blockers"]
    cov = client.get(f"{API}/options/coverage").json()
    assert cov["backtest_mode"] == "unavailable" and cov["underlying"] == "NIFTY"
    bad = copy.deepcopy(c)
    bad["options"]["selection"]["delta_min"] = 0.9
    assert post(client, "/validate", {"definition": bad}, 200).json()["valid"] is False
    assert post(client, "/options/compile", {"ema_fast": 1}).status_code == 422


def test_backtest_and_live_scan_share_the_same_conditions(client, engine):
    from app.services import intraday_runner as runner
    from app.models.strategies import IntradaySignal, OptionContract, OptionQuote, Strategy, StrategyVersion
    login(client, engine)
    with Session(engine) as db:
        fut0 = Instrument(symbol="NIFTY25JANFUT", display_name="NIFTY FUT", asset_class="future", venue="NSE", currency="INR", country="IN", timezone="Asia/Kolkata", multiplier=D(25), expiry=E1.date(), created_at=T0)
        db.add(fut0)
        db.commit()
    post(client, "/starters/install", {}, 200)
    f = mk_market(2)
    d = make_def()
    full = evaluate(d, f, EQUITY_IN)
    i = int(np.flatnonzero(full.signals["long_entry"])[0])
    sig_t = (f["time"].iloc[i] + pd.Timedelta(minutes=1)).to_pydatetime()
    with Session(engine) as db:
        fut = db.scalar(select(Instrument).where(Instrument.symbol == "NIFTY25JANFUT"))
        sub = f.iloc[: i + 1]
        db.add_all(MarketBar(instrument_id=fut.id, interval="1m", timestamp=r.time.to_pydatetime(), open=D(repr(round(r.open, 2))), high=D(repr(round(r.high, 2))), low=D(repr(round(r.low, 2))),
                             close=D(repr(round(r.close, 2))), volume=D(repr(r.volume)), source="test", retrieved_at=T0, is_synthetic=False) for r in sub.itertuples())
        exp = sig_t + timedelta(days=3)
        cs = []
        for strike in (22000 + 100 * k for k in range(-5, 6)):
            oc = OptionContract(underlying="NIFTY", trading_symbol=f"NIFTY {strike} CE", expiry=exp, strike=D(strike), option_type="CE", lot_size=25, valid_from=sig_t - timedelta(days=5), source="test", master_version="t1", ingested_at=T0)
            db.add(oc)
            db.flush()
            db.add(OptionQuote(contract_id=oc.id, quote_time=sig_t - timedelta(seconds=3), ingested_at=T0, bid=D(100), ask=D("100.5"), bid_qty=500, ask_qty=500, delta=D("0.52"), greeks_source="provider", source="test"))
            cs.append(oc)
        db.commit()
        us = db.scalar(select(Strategy).where(Strategy.name == "NIFTY Momentum Options Scalper"))
        v = db.scalar(select(StrategyVersion).where(StrategyVersion.strategy_id == us.id))
        us.status = "active"
        db.commit()
        defn = StrategyDefinition.model_validate(v.definition)
        rows = runner.scan_instrument(db, us, v, defn, fut, sig_t)
        db.commit()
        longs = [(r.signal_time, r.option, r.status) for r in rows if r.direction == "long"]
    assert full.signals["long_entry"][i]                                                         # the backtest-side evaluation fires at this candle ...
    assert longs and longs[0][0].replace(tzinfo=timezone.utc) == sig_t                           # ... and the live scan, on the same data, fires at the same candle
    o = longs[0][1]
    assert o["mode"] == "decision_support_only" and o["option_type"] == "CE" and D(o["entry_reference_ask"]) == D("100.5") and o["premium_stop"] and o["premium_target"] and o["why"]
    assert o["delta_source"] == "provider" and D(o["bid"]) == D("100") and D(o["ask"]) == D("100.5") and o["max_hold_minutes"] == 5.0
