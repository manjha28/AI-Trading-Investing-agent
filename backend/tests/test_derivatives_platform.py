"""Dhan derivatives platform: master parsing/refresh, payload parsers, idempotent stores, job engine, planners, coverage, API, options integration.
Synthetic fixtures only (invented symbols/ids/lot sizes: nothing may depend on real contract specifications)."""
import csv
import io
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D
from uuid import UUID

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.broker_adapters import BrokerAdapterError, BrokerRateLimited
from app.models import (DerivativeContract, DerivativeIngestionJob, DerivativeIngestionTask, DerivativeInstrument, FuturesCandle1d, FuturesCandle1m, Instrument, OptionCandle1m, OptionChainSnapshot,
                        RollingOptionCandle1m)
from app.models.strategies import OptionContract, OptionQuote
from app.services import derivatives_ingest as di
from app.services import dhan_derivatives as dd
from app.services.options_data import DbOptionData, coverage_report
from tests.test_signals_lab_api import client, engine, h, login  # noqa: F401  (fixtures + helpers)

NOW = datetime(2026, 11, 5, 7, 0, tzinfo=timezone.utc)                      # 12:30 IST
COLS = ["EXCH_ID", "SEGMENT", "SECURITY_ID", "ISIN", "INSTRUMENT", "UNDERLYING_SECURITY_ID", "UNDERLYING_SYMBOL", "SYMBOL_NAME", "DISPLAY_NAME", "INSTRUMENT_TYPE", "SERIES", "LOT_SIZE", "SM_EXPIRY_DATE",
        "STRIKE_PRICE", "OPTION_TYPE", "TICK_SIZE", "EXPIRY_FLAG"]


def row(**kw):
    base = {c: "" for c in COLS} | {"ISIN": "NA", "SERIES": "NA", "OPTION_TYPE": "XX", "STRIKE_PRICE": "-0.01000", "INSTRUMENT_TYPE": ""}
    return base | {k: str(v) for k, v in kw.items()}


def master(rows):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=COLS)
    w.writeheader()
    w.writerows(rows)
    return [ln + "\n" for ln in buf.getvalue().splitlines()]


def fno(exch="NSE", seg="D", ins="OPTIDX", sid="9001", usid="777", sym="ZEDX", name=None, expiry="2026-11-26", strike="1000", ot="CE", lot="40", tick="0.05", flag="W"):
    return row(EXCH_ID=exch, SEGMENT=seg, SECURITY_ID=sid, INSTRUMENT=ins, UNDERLYING_SECURITY_ID=usid, UNDERLYING_SYMBOL=sym, SYMBOL_NAME=name or f"{sym}-{expiry}-{strike}-{ot}", LOT_SIZE=lot,
               SM_EXPIRY_DATE=expiry, STRIKE_PRICE=strike if ins.startswith("OPT") else "-0.01000", OPTION_TYPE=ot if ins.startswith("OPT") else "XX", TICK_SIZE=tick, EXPIRY_FLAG=flag)


def base_master():
    return [
        row(EXCH_ID="NSE", SEGMENT="I", SECURITY_ID="501", INSTRUMENT="INDEX", UNDERLYING_SYMBOL="ZEDX", SYMBOL_NAME="ZEDX"),
        row(EXCH_ID="NSE", SEGMENT="E", SECURITY_ID="2001", INSTRUMENT="EQUITY", SERIES="EQ", UNDERLYING_SYMBOL="ACME", SYMBOL_NAME="ACME LTD"),
        row(EXCH_ID="NSE", SEGMENT="E", SECURITY_ID="2002", INSTRUMENT="EQUITY", SERIES="EQ", UNDERLYING_SYMBOL="NOFNO", SYMBOL_NAME="NO DERIVATIVES LTD"),   # equity only: NOT F&O eligible
        row(EXCH_ID="BSE", SEGMENT="I", SECURITY_ID="601", INSTRUMENT="INDEX", UNDERLYING_SYMBOL="BZEDX", SYMBOL_NAME="BZEDX"),
        fno(ins="FUTIDX", sid="9100", sym="ZEDX", expiry="2026-11-26", lot="55", tick="0.1", name="ZEDX-Nov2026-FUT", flag=""),
        fno(ins="FUTIDX", sid="9101", sym="ZEDX", expiry="2026-12-31", lot="55", tick="0.1", name="ZEDX-Dec2026-FUT", flag=""),
        fno(ins="OPTIDX", sid="9001", sym="ZEDX", strike="1000", ot="CE"),
        fno(ins="OPTIDX", sid="9002", sym="ZEDX", strike="1000", ot="PE"),
        fno(ins="OPTIDX", sid="9003", sym="ZEDX", strike="1030", ot="CE"),                                   # 30-point strike spacing: nothing assumes 50/100
        fno(ins="FUTSTK", sid="9200", usid="888", sym="ACME", expiry="2026-11-26", lot="1375", name="ACME-Nov2026-FUT", flag=""),
        fno(ins="OPTSTK", sid="9201", usid="888", sym="ACME", strike="250", ot="CE", lot="1375", flag="M"),
        fno(exch="BSE", seg="D", ins="OPTIDX", sid="7001", usid="55", sym="BZEDX", strike="500", ot="CE", lot="20"),
        fno(exch="MCX", seg="M", ins="FUTCOM", sid="8001", usid="114", sym="GLD", expiry="2026-12-05", lot="1", tick="1", name="GLD-Dec-FUT", flag=""),
        fno(exch="MCX", seg="M", ins="OPTFUT", sid="8002", usid="114", sym="GLD", expiry="2026-12-05", strike="70000", ot="PE", lot="1", tick="1", flag="M"),
        fno(exch="NSE", seg="M", ins="OPTFUT", sid="8101", usid="99", sym="NGLD", expiry="2026-12-05", strike="10", ot="CE"),
        fno(exch="NSE", seg="C", ins="FUTCUR", sid="8201", usid="3", sym="UXD", expiry="2026-11-27", lot="1000", name="UXD-Nov-FUT", flag=""),
    ]


def parsed(rows=None):
    return dd.parse_master(master(rows or base_master()))


# ───────────── master parsing / classification ─────────────
def test_master_classifies_every_exchange_segment_and_category_without_hardcoded_specs():
    p = parsed()
    by = {(c.exchange, c.segment, c.instrument_type): c for c in p.contracts}
    assert by[("NSE", "NSE_FNO", "FUTIDX")].category == "index" and by[("NSE", "NSE_FNO", "FUTSTK")].category == "stock"
    assert by[("BSE", "BSE_FNO", "OPTIDX")].category == "index" and by[("MCX", "MCX_COMM", "FUTCOM")].category == "commodity" and by[("NSE", "NSE_CURRENCY", "FUTCUR")].category == "currency"
    assert by[("NSE", "NSE_COMM", "OPTFUT")].segment == "NSE_COMM"                                        # NSE commodity options are classified (support is a separate capability)
    zed = next(c for c in p.contracts if c.security_id == "9100")
    assert (zed.lot_size, zed.tick_size, zed.expiry, zed.underlying_security_id) == (D("55"), D("0.1"), date(2026, 11, 26), "777")      # from the master, nothing else
    assert {c.strike for c in p.contracts if c.instrument_type == "OPTIDX" and c.underlying_symbol == "ZEDX"} == {D(1000), D(1030)}
    assert p.rows_total == len(base_master()) and p.skipped == {} and len(p.sha256) == 64


def test_cash_underlying_resolution_and_fno_eligibility_come_from_listed_derivatives():
    p = parsed()
    u = {k[2]: v for k, v in p.underlyings.items()}
    assert u["ZEDX"]["cash_security_id"] == "501" and u["ZEDX"]["cash_segment"] == "IDX_I" and u["ZEDX"]["has_futures"] and u["ZEDX"]["has_options"]
    assert u["ACME"]["cash_security_id"] == "2001" and u["ACME"]["cash_segment"] == "NSE_EQ"             # the equity security, NOT the master's F&O underlying id 888
    assert u["GLD"]["cash_security_id"] == "114" and u["GLD"]["cash_segment"] == "MCX_COMM"
    assert "NOFNO" not in u and not any(k[2] == "NOFNO" for k in p.underlyings)                          # an equity-master stock is not F&O eligible by itself
    assert u["BZEDX"]["cash_security_id"] == "601"


def test_master_parsing_fails_loudly_and_skips_bad_rows_without_inventing_them():
    with pytest.raises(dd.MasterError):
        dd.parse_master(["EXCH_ID,SEGMENT\n", "NSE,D\n"])
    rows = base_master() + [fno(sid="x1", sym="BAD"), fno(sid="9999", sym="BAD2", expiry="not-a-date"), fno(exch="XXX", seg="Z", sid="9998", sym="BAD3"), fno(sid="9997", sym="BAD4", ot="XX")]
    p = parsed(rows)
    assert not any(c.underlying_symbol.startswith("BAD") for c in p.contracts) and sum(p.skipped.values()) == 4


# ───────────── master refresh: history, expiry, removal ─────────────
def refresh(engine, rows, now):
    with Session(engine) as db:
        return di.refresh_master(db, parsed(rows), now)


def test_initial_refresh_persists_underlyings_futures_and_options_in_the_right_tables(client, engine):
    r = refresh(engine, base_master(), NOW)
    assert r["underlyings"] == 6 and r["new"] == 12 and r["changed"] == r["removed"] == r["expired"] == 0
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(DerivativeContract)) == 5                      # 2 ZEDX + 1 ACME + 1 GLD + 1 UXD futures; options: 3 ZEDX + ACME + BZEDX + GLD + NGLD
        assert db.scalar(select(func.count()).select_from(OptionContract)) == 7
        c = db.scalar(select(OptionContract).where(OptionContract.security_id == "9003"))
        assert (c.exchange, c.segment, c.underlying, c.strike, c.option_type, c.lot_size, c.tick_size, c.expiry_flag, c.status) == ("NSE", "NSE_FNO", "ZEDX", D(1030), "CE", 40, D("0.05"), "W", "active")
        assert c.provider_id == "9003|NSE_FNO" and c.expiry.astimezone(dd.IST).date() == date(2026, 11, 26) and c.valid_to is None and c.master_version.startswith("dhan-master-")
        assert db.scalar(select(func.count()).select_from(DerivativeInstrument).where(DerivativeInstrument.has_options.is_(True))) == 5
        snap = di.inventory(db)["master"]
        assert snap["derivative_rows"] == 12 and snap["new"] == 12


def test_refresh_is_idempotent_and_detects_nothing_when_nothing_changed(client, engine):
    refresh(engine, base_master(), NOW)
    again = refresh(engine, base_master(), NOW + timedelta(days=1))
    assert (again["new"], again["changed"], again["removed"], again["expired"]) == (0, 0, 0, 0) and again["unchanged"] is True
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(OptionContract)) == 7 and db.scalar(select(func.count()).select_from(DerivativeContract)) == 5


def test_lot_size_change_preserves_the_old_specification_as_history(client, engine):
    refresh(engine, base_master(), NOW)
    changed = [r if r["SECURITY_ID"] != "9001" else r | {"LOT_SIZE": "75"} for r in base_master()]
    changed = [r if r["SECURITY_ID"] != "9100" else r | {"LOT_SIZE": "60"} for r in changed]
    res = refresh(engine, changed, NOW + timedelta(days=2))
    assert res["changed"] == 2 and res["new"] == 0
    with Session(engine) as db:
        opts = list(db.scalars(select(OptionContract).where(OptionContract.security_id == "9001").order_by(OptionContract.valid_from)))
        assert [(o.lot_size, o.valid_to is not None) for o in opts] == [(40, True), (75, False)]          # the old row is CLOSED, not overwritten
        assert opts[0].valid_to.replace(tzinfo=timezone.utc) == NOW + timedelta(days=2)
        futs = list(db.scalars(select(DerivativeContract).where(DerivativeContract.security_id == "9100").order_by(DerivativeContract.valid_from)))
        assert [f.lot_size for f in futs] == [D("55"), D("60")]
        # historical selection sees the specification that applied at the time
        src = DbOptionData(db)
        old = [c for c in src.contracts_as_of("ZEDX", NOW + timedelta(days=1)) if c.symbol.endswith("1000-CE")]
        new = [c for c in src.contracts_as_of("ZEDX", NOW + timedelta(days=3)) if c.symbol.endswith("1000-CE")]
        assert old[0].lot_size == 40 and new[0].lot_size == 75


def test_expiry_transition_removed_and_new_contracts_are_detected(client, engine):
    refresh(engine, base_master(), NOW)
    later = NOW + timedelta(days=22)                                                                     # 2026-11-27: the Nov-26 contracts are past expiry
    rows = [r for r in base_master() if r["SECURITY_ID"] != "9003"] + [fno(ins="OPTIDX", sid="9004", sym="ZEDX", strike="1060", ot="CE", expiry="2027-01-28")]
    res = refresh(engine, rows, later)
    assert res["removed"] == 1 and res["new"] == 1 and res["expired"] >= 2
    with Session(engine) as db:
        st = {c.security_id: c.status for c in db.scalars(select(OptionContract))}
        assert st["9003"] == "removed" and st["9004"] == "active" and st["9001"] == "expired"             # listed but past expiry -> expired
        fut = {c.security_id: c.status for c in db.scalars(select(DerivativeContract))}
        assert fut["9100"] == "expired" and fut["9101"] == "active"


# ───────────── payload parsers / windows ─────────────
def payload(n=3, base=100, oi=True, ts0=1_762_300_000):
    d = {k: [base + i for i in range(n)] for k in ("open", "high", "low", "close")}
    d["high"] = [x + 2 for x in d["high"]]
    d["low"] = [x - 2 for x in d["low"]]
    d["volume"] = [10 * (i + 1) for i in range(n)]
    d["timestamp"] = [ts0 + 60 * i for i in range(n)]
    if oi:
        d["open_interest"] = [500 + i for i in range(n)]
    return d


def test_candle_parser_validates_alignment_skips_inconsistent_candles_and_keeps_oi_optional():
    rows = dd.parse_candles(payload())
    assert len(rows) == 3 and rows[0].ts == datetime.fromtimestamp(1_762_300_000, timezone.utc) and rows[0].open_interest == 500 and rows[0].volume == 10
    assert dd.parse_candles(payload(oi=False))[0].open_interest is None
    bad = payload()
    bad["close"][1] = 999                                                                                # close above high
    r = dd.parse_candles(bad)
    assert len(r) == 2 and r.rejected == 1 and r.rejected_examples[0]["ohlc"][3] == "999"
    short = payload()
    short["volume"] = short["volume"][:2]
    with pytest.raises(dd.PayloadError):
        dd.parse_candles(short)
    with pytest.raises(dd.PayloadError):
        dd.parse_candles({"open": []})


def test_rolling_parser_keeps_strike_spot_iv_oi_and_treats_absent_side_as_empty():
    side = payload() | {"oi": [1, 2, 3], "iv": [9.5, 9.6, 9.7], "spot": [24000.0, 24001.0, 24002.0], "strike": [24050.0, 24050.0, 24100.0]}
    side.pop("open_interest")
    rows = dd.parse_rolling({"ce": side, "pe": None}, "CE")
    assert [r.strike for r in rows] == [D(24050), D(24050), D(24100)] and rows[0].iv == D("9.5") and rows[0].spot == D(24000) and rows[0].open_interest == 1
    assert dd.parse_rolling({"ce": side, "pe": None}, "PE") == [] and dd.parse_rolling({}, "CE") == []
    with pytest.raises(dd.PayloadError):
        dd.parse_rolling({"ce": side | {"iv": [1.0]}}, "CE")


def test_chain_parser_never_turns_missing_prices_into_zero():
    data = {"last_price": 24010.5, "oc": {"24000.000000": {"ce": {"security_id": 111, "last_price": 150.0, "top_bid_price": 0, "top_ask_price": 150.5, "top_bid_quantity": 0, "top_ask_quantity": 75, "volume": 9, "oi": 100,
                                                               "previous_oi": 90, "previous_volume": 8, "implied_volatility": 11.2, "greeks": {"delta": 0.52, "gamma": 0.001, "theta": -5.2, "vega": 9.1}},
                                                         "pe": {"security_id": 112, "last_price": 90.0, "top_bid_price": 89.5, "top_ask_price": 90.5, "greeks": {}}}}}
    price, qs = dd.parse_chain(data)
    ce = next(q for q in qs if q.option_type == "CE")
    pe = next(q for q in qs if q.option_type == "PE")
    assert price == D("24010.5") and ce.bid is None and ce.ask == D("150.5") and ce.delta == D("0.52") and ce.previous_oi == 90 and pe.delta is None and pe.bid == D("89.5")
    with pytest.raises(dd.PayloadError):
        dd.parse_chain({"last_price": 1})


def test_request_windows_respect_the_documented_and_measured_limits():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    w = dd.intraday_windows(start, start + timedelta(days=200))
    assert all((b - a) < timedelta(days=dd.INTRADAY_MAX_DAYS) for a, b in w) and w[0][0] == start and w[-1][1] == start + timedelta(days=200) and all(w[i][1] == w[i + 1][0] for i in range(len(w) - 1))
    rw = dd.rolling_windows(date(2026, 10, 1), date(2026, 10, 10))
    assert rw == [(date(2026, 10, 1), date(2026, 10, 2)), (date(2026, 10, 3), date(2026, 10, 4)), (date(2026, 10, 5), date(2026, 10, 6)), (date(2026, 10, 7), date(2026, 10, 8)), (date(2026, 10, 9), date(2026, 10, 10))]
    assert all((b - a).days <= dd.ROLLING_MAX_SPAN_DAYS for a, b in rw) and dd.rolling_windows(date(2026, 10, 3), date(2026, 10, 1)) == []
    assert dd.strike_offsets(2) == ["ATM", "ATM+1", "ATM-1", "ATM+2", "ATM-2"]


# ───────────── stores: idempotency, identity, timestamps ─────────────
@pytest.fixture
def world(client, engine):
    refresh(engine, base_master(), NOW)
    return engine


def contract_id(engine, sid, model=DerivativeContract):
    with Session(engine) as db:
        return db.scalar(select(model.id).where(model.security_id == sid))


def test_futures_candle_store_is_idempotent_utc_aligned_and_excludes_the_forming_candle(world):
    cid = contract_id(world, "9100")
    ts0 = int((NOW - timedelta(minutes=10)).timestamp())
    rows = dd.parse_candles(payload(5, ts0=ts0 - ts0 % 60))
    forming = dd.CandleRow(NOW - timedelta(seconds=20), D(1), D(2), D(1), D(2), 1, 1)                  # still open: must not be stored
    with Session(world) as db:
        assert di.store_futures_candles(db, cid, list(rows) + [forming], NOW) == 5
        assert di.store_futures_candles(db, cid, list(rows) + [forming], NOW) == 0                      # replaying a window adds nothing
        db.commit()
        got = db.scalars(select(FuturesCandle1m).order_by(FuturesCandle1m.ts)).all()
        assert len(got) == 5 and got[0].ts.replace(tzinfo=timezone.utc) == rows[0].ts and got[0].open_interest == 500 and all(g.source == "dhan_intraday" for g in got)


def test_active_option_candles_and_rolling_candles_are_separate_stores_with_distinct_identity(world):
    ocid = contract_id(world, "9001", OptionContract)
    ts0 = 1_762_300_020
    with Session(world) as db:
        assert di.store_option_candles(db, ocid, dd.parse_candles(payload(3, ts0=ts0)), NOW) == 3
        req = {"segment": "NSE_FNO", "from": "2025-10-01"}
        side = payload(2, ts0=ts0) | {"oi": [1, 2], "iv": [9.5, 9.6], "spot": [100.0, 101.0], "strike": [24050.0, 24100.0]}
        side.pop("open_interest")
        r1 = dd.parse_rolling({"ce": side}, "CE")
        common = dict(exchange="NSE", segment="NSE_FNO", cash_security_id="501", symbol="ZEDX", instrument="OPTIDX", expiry_flag="WEEK", expiry_code=1, option_type="CE", request=req, now=NOW)
        assert di.store_rolling(db, strike_offset="ATM", rows=r1, **common) == 2
        assert di.store_rolling(db, strike_offset="ATM", rows=r1, **common) == 0                         # idempotent
        assert di.store_rolling(db, strike_offset="ATM+1", rows=r1, **common) == 2                        # same timestamps, different offset: a DIFFERENT series
        db.commit()
        assert db.scalar(select(func.count()).select_from(OptionCandle1m)) == 3 and db.scalar(select(func.count()).select_from(RollingOptionCandle1m)) == 4
        atm = db.scalars(select(RollingOptionCandle1m).where(RollingOptionCandle1m.strike_offset == "ATM").order_by(RollingOptionCandle1m.ts)).all()
        assert [a.strike for a in atm] == [D(24050), D(24100)]                                           # the absolute strike moved; offset stays the series identity
        assert atm[0].request == req and atm[0].implied_volatility == D("9.5") and atm[0].spot == D(100) and atm[0].source == "dhan_rollingoption"
        assert db.scalar(select(func.count()).select_from(RollingOptionCandle1m).where(RollingOptionCandle1m.expiry_code == 1)) == 4


def test_daily_series_kinds_are_never_mixed(world):
    cid = contract_id(world, "9100")
    day_ts = int(datetime(2026, 10, 1, tzinfo=dd.IST).timestamp())
    rows = dd.parse_candles(payload(2, ts0=day_ts) | {"timestamp": [day_ts, day_ts + 86400]})
    with Session(world) as db:
        assert di.store_daily(db, exchange="NSE", segment="NSE_FNO", security_id="9100", series_kind="provider_rolled", expiry_code=0, contract_id=None, rows=rows, now=NOW) == 2
        assert di.store_daily(db, exchange="NSE", segment="NSE_FNO", security_id="9100", series_kind="contract", expiry_code=0, contract_id=cid, rows=rows, now=NOW) == 2
        db.commit()
        kinds = {(d.series_kind, d.day) for d in db.scalars(select(FuturesCandle1d))}
        assert kinds == {("provider_rolled", date(2026, 10, 1)), ("provider_rolled", date(2026, 10, 2)), ("contract", date(2026, 10, 1)), ("contract", date(2026, 10, 2))}   # IST session dates


def chain_data(prices=None):
    sec = {"1": ("9001", "CE"), "2": ("9002", "PE"), "3": ("9003", "CE")}
    oc: dict = {"1000.000000": {"ce": {"security_id": 9001, "last_price": 150.0, "top_bid_price": 149.5, "top_ask_price": 150.5, "top_bid_quantity": 400, "top_ask_quantity": 320, "volume": 10, "oi": 1000,
                                       "previous_oi": 900, "previous_volume": 5, "implied_volatility": 11.0, "greeks": {"delta": 0.51, "gamma": 0.002, "theta": -4.0, "vega": 8.0}},
                                 "pe": {"security_id": 9002, "last_price": 100.0, "top_bid_price": 0, "top_ask_price": 100.5, "greeks": {"delta": -0.49}}},
                "1030.000000": {"ce": {"security_id": 424242, "last_price": 50.0, "top_bid_price": 49.0, "top_ask_price": 51.0, "greeks": {"delta": 0.4}}}}      # security id NOT in the master
    return {"last_price": 1004.5, "oc": oc}


def test_chain_snapshot_persists_greeks_bid_ask_and_never_invents_unlisted_contracts(world):
    price, quotes = dd.parse_chain(chain_data())
    with Session(world) as db:
        u = db.scalar(select(DerivativeInstrument).where(DerivativeInstrument.underlying_symbol == "ZEDX"))
        res = di.store_chain_snapshot(db, underlying=u, expiry=date(2026, 11, 26), price=price, quotes=quotes, captured_at=NOW)
        db.commit()
        assert res["quotes"] == 3 and res["stored"] == 2 and res["unmatched_to_master"] == 1 and res["with_bid_ask"] == 1
        qs = {q.contract_id: q for q in db.scalars(select(OptionQuote))}
        ce = next(q for q in qs.values() if q.delta == D("0.51"))
        assert ce.bid == D("149.5") and ce.ask == D("150.5") and ce.bid_qty == 400 and ce.gamma == D("0.002") and ce.theta == D("-4.0") and ce.previous_open_interest == 900 and ce.underlying_price == D("1004.5")
        assert ce.greeks_source == "provider" and ce.quote_time.replace(tzinfo=timezone.utc) == NOW and ce.snapshot_id is not None
        pe = next(q for q in qs.values() if q.delta == D("-0.49"))
        assert pe.bid is None and pe.ask == D("100.5")                                                    # zero bid stored as missing, never as 0
        snap = db.scalar(select(OptionChainSnapshot))
        assert (snap.strikes, snap.quotes_stored, snap.quotes_with_bid_ask, snap.expiry) == (2, 2, 1, date(2026, 11, 26))
        assert db.scalar(select(func.count()).select_from(OptionContract)) == 7                           # no contract was created from the chain


# ───────────── job engine ─────────────
class FakeClient:
    def __init__(self, fail=None, empty=()):
        self.calls, self.fail, self.empty = [], fail or {}, set(empty)

    def intraday(self, *, segment, security_id, instrument, start, end, oi=True, interval="1"):
        self.calls.append(("intraday", security_id))
        err = self.fail.get(security_id)
        if err:
            raise err() if callable(err) else err
        b = dd.CandleBatch(() if security_id in self.empty else dd.parse_candles(payload(4, ts0=int((NOW - timedelta(hours=1)).timestamp()) // 60 * 60)))
        b.rejected, b.rejected_examples = 0, []
        return b


def sf_for(engine):
    return lambda: Session(engine)


def make_job(engine, sids, kind="futures_intraday", model=DerivativeContract):
    with Session(engine) as db:
        tasks = []
        for sid in sids:
            cid = db.scalar(select(model.id).where(model.security_id == sid))
            tasks.append(di.TaskSpec(f"t:{sid}", kind, {"contract_id": str(cid), "start": (NOW - timedelta(days=2)).isoformat(), "end": NOW.isoformat()}))
        return di.create_job(db, "futures_backfill", {}, tasks).id


def counts(engine, job_id):
    with Session(engine) as db:
        j = db.get(DerivativeIngestionJob, job_id)
        t = dict(db.execute(select(DerivativeIngestionTask.status, func.count()).where(DerivativeIngestionTask.job_id == job_id).group_by(DerivativeIngestionTask.status)).all())
        return j.status, t, j.rows_written, j.tasks_done, j.tasks_failed


def test_job_runs_to_completion_reports_progress_and_is_idempotent_on_replay(world):
    job = make_job(world, ["9100", "9101", "9200"])
    client = FakeClient(empty=["9200"])
    assert di.run_job(sf_for(world), job, client, now_fn=lambda: NOW) == "succeeded"
    status, t, rows, done, failed = counts(world, job)
    assert (status, t, rows, done, failed) == ("succeeded", {"done": 2, "empty": 1}, 8, 3, 0)            # an empty response is a result, not a failure
    job2 = make_job(world, ["9100"])
    di.run_job(sf_for(world), job2, FakeClient(), now_fn=lambda: NOW)
    assert counts(world, job2)[2] == 0                                                                    # the same candles again: nothing duplicated
    with Session(world) as db:
        assert db.scalar(select(func.count()).select_from(FuturesCandle1m)) == 8


def test_transient_failures_retry_with_backoff_and_permanent_failures_go_dead(world):
    job = make_job(world, ["9100", "9101"])
    clock = [NOW]
    client = FakeClient(fail={"9100": lambda: BrokerRateLimited("slow down"), "9101": lambda: BrokerAdapterError("bad request", status_code=400)})
    di.run_job(sf_for(world), job, client, now_fn=lambda: clock[0], max_tasks=2)
    with Session(world) as db:
        t = {x.task_key: x for x in db.scalars(select(DerivativeIngestionTask).where(DerivativeIngestionTask.job_id == job))}
        assert t["t:9100"].status == "failed" and t["t:9100"].attempts == 1 and t["t:9100"].next_attempt_at.replace(tzinfo=timezone.utc) == NOW + timedelta(seconds=di.BACKOFF_SECONDS[0])
        assert t["t:9101"].status == "dead" and t["t:9101"].attempts == 1 and "400" in t["t:9101"].error                # a 400 is permanent: no retry
    before = len(client.calls)
    di.run_job(sf_for(world), job, client, now_fn=lambda: clock[0], max_tasks=3)
    assert len(client.calls) == before                                                                   # backoff not elapsed: not retried yet
    clock[0] = NOW + timedelta(seconds=di.BACKOFF_SECONDS[0] + 1)
    client.fail.pop("9100")                                                                              # the provider recovers
    assert di.run_job(sf_for(world), job, client, now_fn=lambda: clock[0]) == "partial"
    status, t, rows, done, failed = counts(world, job)
    assert (status, t, done, failed) == ("partial", {"done": 1, "dead": 1}, 1, 1) and rows == 4


def test_retries_are_bounded_and_end_in_dead_letter_status(world):
    job = make_job(world, ["9100"])
    clock = [NOW]
    client = FakeClient(fail={"9100": lambda: BrokerRateLimited("limited")})
    for _ in range(di.MAX_ATTEMPTS + 1):
        di.run_job(sf_for(world), job, client, now_fn=lambda: clock[0], max_tasks=1)
        clock[0] += timedelta(hours=1)
    status, t, *_ = counts(world, job)
    assert t == {"dead": 1} and len(client.calls) == di.MAX_ATTEMPTS
    di.run_job(sf_for(world), job, client, now_fn=lambda: clock[0])
    assert counts(world, job)[0] == "failed"


def test_interrupted_job_resumes_without_repeating_finished_work(world):
    job = make_job(world, ["9100", "9101", "9200"])
    c1 = FakeClient()
    di.run_job(sf_for(world), job, c1, now_fn=lambda: NOW, max_tasks=1)                                  # the process "dies" after one task
    assert counts(world, job)[1] == {"done": 1, "pending": 2} and len(c1.calls) == 1
    c2 = FakeClient()
    assert di.run_job(sf_for(world), job, c2, now_fn=lambda: NOW) == "succeeded"
    assert len(c2.calls) == 2 and counts(world, job)[1] == {"done": 3}


def test_a_task_left_running_by_a_crashed_worker_is_recovered(world):
    job = make_job(world, ["9100"])
    with Session(world) as db:
        t = db.scalar(select(DerivativeIngestionTask).where(DerivativeIngestionTask.job_id == job))
        t.status, t.started_at, t.attempts = "running", NOW - timedelta(hours=2), 1
        db.commit()
    assert di.run_job(sf_for(world), job, FakeClient(), now_fn=lambda: NOW) == "succeeded"


def test_cancellation_stops_a_job_and_marks_remaining_tasks(world):
    job = make_job(world, ["9100", "9101", "9200"])
    di.run_job(sf_for(world), job, FakeClient(), now_fn=lambda: NOW, max_tasks=1)
    with Session(world) as db:
        assert di.cancel_job(db, job) is True
    assert di.run_job(sf_for(world), job, FakeClient(), now_fn=lambda: NOW) == "cancelled"
    status, t, *_ = counts(world, job)
    assert status == "cancelled" and t == {"done": 1, "cancelled": 2}
    with Session(world) as db:
        assert di.cancel_job(db, job) is False                                                            # already finished


# ───────────── planners / estimates ─────────────
def test_planners_use_measured_capabilities_prioritise_and_estimate_without_calling_dhan(world):
    with Session(world) as db:
        tasks = di.plan_futures(db, {"segment": "NSE_FNO"}, lookback_days=89, now=NOW)
        kinds = [t.kind for t in tasks]
        assert kinds.count("futures_intraday") == 3 and kinds.count("futures_daily") == 2                  # daily: one per underlying (ZEDX, ACME), not per expiry
        assert all(t.params["segment"] if "segment" in t.params else True for t in tasks)
        cur = di.plan_futures(db, {"segment": "NSE_CURRENCY"}, now=NOW)
        assert cur == []                                                                                   # currency history endpoints are unverified: no blind requests
        roll = di.plan_rolling(db, {}, first=date(2026, 10, 1), last=date(2026, 10, 4), max_offset=1)
        flags = {(t.params["symbol"], t.params["instrument"], t.params["expiry_flag"]) for t in roll}
        assert ("ZEDX", "OPTIDX", "WEEK") in flags and ("ZEDX", "OPTIDX", "MONTH") in flags and ("ACME", "OPTSTK", "MONTH") in flags
        assert ("ACME", "OPTSTK", "WEEK") not in flags and not any(s == "NGLD" for s, *_ in flags)       # stock weekly and NSE_COMM are unsupported (measured)
        assert not any(t.params["from"] < dd.ROLLING_EARLIEST.isoformat() for t in roll)
        assert all(date.fromisoformat(t.params["to"]) - date.fromisoformat(t.params["from"]) <= timedelta(days=dd.ROLLING_MAX_SPAN_DAYS) for t in roll)
        # BZEDX is a real BSE index underlying in this fixture and the rolling API
        # supports its OPTIDX series. Include both BSE expiry flags; GLD is the
        # supported MCX monthly OPTFUT series. Every planned combination is unique.
        series = {
            ("NSE", "ZEDX", "OPTIDX", "WEEK"), ("NSE", "ZEDX", "OPTIDX", "MONTH"),
            ("NSE", "ACME", "OPTSTK", "MONTH"),
            ("BSE", "BZEDX", "OPTIDX", "WEEK"), ("BSE", "BZEDX", "OPTIDX", "MONTH"),
            ("MCX", "GLD", "OPTFUT", "MONTH"),
        }
        windows = dd.rolling_windows(date(2026, 10, 1), date(2026, 10, 4))
        offsets = dd.strike_offsets(1)
        expected = {
            (exchange, symbol, instrument, flag, offset, option_type, start.isoformat(), end.isoformat())
            for exchange, symbol, instrument, flag in series
            for offset in offsets for option_type in ("CE", "PE") for start, end in windows
        }
        actual = {
            (t.params["exchange"], t.params["symbol"], t.params["instrument"], t.params["expiry_flag"],
             t.params["strike_offset"], t.params["option_type"], t.params["from"], t.params["to"])
            for t in roll
        }
        assert {t.key for t in roll} == {f"roll:{e}:{s}:{i}:{flag}:1:{off}:{ot}:{start.replace('-', '')}" for e, s, i, flag, off, ot, start, _ in expected}
        assert actual == expected and len(roll) == 72
        est = di.estimate_requests(roll)
        assert est["requests"] == len(roll) and est["min_seconds"] == len(roll)
        chains = di.plan_chains(db, priority_underlyings=["GLD", "ACME"], expiry_count=1)
        assert [t.params["instrument_id"] for t in chains[:2]] == [str(db.scalar(select(DerivativeInstrument.id).where(DerivativeInstrument.underlying_symbol == s))) for s in ("GLD", "ACME")]
        assert di.estimate_requests(chains)["min_seconds"] == int(len(chains) * dd.CHAIN_MIN_INTERVAL_SECONDS)
        assert di.plan_option_candles_near_atm(db, "ZEDX", exchange="NSE") == []                           # no snapshot yet: nothing to anchor ATM on, nothing invented


def test_near_atm_option_candle_plan_follows_the_captured_underlying_price(world):
    price, quotes = dd.parse_chain(chain_data())
    with Session(world) as db:
        u = db.scalar(select(DerivativeInstrument).where(DerivativeInstrument.underlying_symbol == "ZEDX"))
        di.store_chain_snapshot(db, underlying=u, expiry=date(2026, 11, 26), price=price, quotes=quotes, captured_at=NOW)
        db.commit()
        tasks = di.plan_option_candles_near_atm(db, "ZEDX", exchange="NSE", half_width=0, now=NOW)
        sids = {db.get(OptionContract, UUID(t.params["contract_id"])).strike for t in tasks}
        assert sids == {D(1000)}                                                                           # 1004.5 is closest to the 1000 strike (30-point spacing comes from the master)


# ───────────── coverage ─────────────
def test_coverage_separates_every_dataset_and_reports_blockers_and_job_status(world):
    cid = contract_id(world, "9100")
    ocid = contract_id(world, "9001", OptionContract)
    with Session(world) as db:
        di.store_futures_candles(db, cid, dd.parse_candles(payload(5, ts0=int((NOW - timedelta(hours=1)).timestamp()) // 60 * 60)), NOW)
        di.store_option_candles(db, ocid, dd.parse_candles(payload(3, ts0=int((NOW - timedelta(hours=1)).timestamp()) // 60 * 60)), NOW)
        side = payload(2) | {"oi": [1, 2], "iv": [9.5, 9.6], "spot": [100.0, 101.0], "strike": [1000.0, 1000.0]}
        side.pop("open_interest")
        di.store_rolling(db, exchange="NSE", segment="NSE_FNO", cash_security_id="501", symbol="ZEDX", instrument="OPTIDX", expiry_flag="WEEK", expiry_code=1, strike_offset="ATM", option_type="CE",
                         rows=dd.parse_rolling({"ce": side}, "CE"), request={}, now=NOW)
        price, quotes = dd.parse_chain(chain_data())
        u = db.scalar(select(DerivativeInstrument).where(DerivativeInstrument.underlying_symbol == "ZEDX"))
        di.store_chain_snapshot(db, underlying=u, expiry=date(2026, 11, 26), price=price, quotes=quotes, captured_at=NOW)
        db.commit()
    job = make_job(world, ["9100"])
    di.run_job(sf_for(world), job, FakeClient(fail={"9100": lambda: BrokerAdapterError("bad request", status_code=400)}), now_fn=lambda: NOW)
    with Session(world) as db:
        assert di.refresh_coverage(db, NOW) > 20
        cov = {(r.data_type, r.exchange, r.segment, r.instrument_type): r for r in db.scalars(select(DerivativeDataCoverage_()))}
        f = cov[("futures_candles_1m", "NSE", "NSE_FNO", "FUTIDX")]
        assert f.row_count == 5 and f.contracts == 2 and f.instruments == 1 and f.sessions == 1 and f.first_ts and f.last_ts and f.last_status == "dead" and "400" in (f.last_error or "")
        assert cov[("futures_candles_1m", "NSE", "NSE_FNO", "FUTSTK")].row_count == 0                           # zero is shown, per category
        assert cov[("option_candles_1m_active", "NSE", "NSE_FNO", "OPTIDX")].row_count == 3
        assert cov[("option_candles_1m_rolling", "NSE", "NSE_FNO", "OPTIDX")].row_count == 2 and cov[("option_candles_1m_rolling", "NSE", "NSE_FNO", "OPTIDX")].contract_state == "rolling"
        assert cov[("option_iv_oi_rolling", "NSE", "NSE_FNO", "OPTIDX")].row_count == 2
        assert cov[("option_chain_snapshots_live", "NSE", "NSE_FNO", "OPTIDX")].row_count == 1 and cov[("option_chain_snapshots_live", "NSE", "NSE_FNO", "OPTIDX")].provenance == "live"
        ba = cov[("option_bid_ask_historical", "NSE", "NSE_FNO", "OPTIDX")]
        assert ba.row_count == 1 and "No historical bid/ask" in ba.blocker                                     # bid/ask exists only from live snapshots
        assert "No expired" in cov[("futures_expired_1m", "NSE", "NSE_FNO", "FUTIDX")].blocker
        assert cov[("option_candles_1m_rolling", "NSE", "NSE_COMM", "OPTFUT")].blocker.startswith("rolling-options API is not available")
        assert len({k[0] for k in cov}) >= 8


def DerivativeDataCoverage_():
    from app.models import DerivativeDataCoverage
    return DerivativeDataCoverage


# ───────────── options-strategy integration ─────────────
def test_options_data_source_and_coverage_use_exact_contract_candles_never_rolling_series(world):
    ocid = contract_id(world, "9001", OptionContract)
    ts0 = int((NOW - timedelta(hours=1)).timestamp()) // 60 * 60
    with Session(world) as db:
        di.store_option_candles(db, ocid, dd.parse_candles(payload(3, ts0=ts0)), NOW)
        side = payload(2, ts0=ts0) | {"oi": [1, 2], "iv": [9.5, 9.6], "spot": [100.0, 101.0], "strike": [1000.0, 1000.0]}
        side.pop("open_interest")
        di.store_rolling(db, exchange="NSE", segment="NSE_FNO", cash_security_id="501", symbol="ZEDX", instrument="OPTIDX", expiry_flag="WEEK", expiry_code=1, strike_offset="ATM", option_type="CE",
                         rows=dd.parse_rolling({"ce": side}, "CE"), request={}, now=NOW)
        db.commit()
        src = DbOptionData(db)
        cs = src.candles_between(str(ocid), NOW - timedelta(hours=2), NOW)
        assert len(cs) == 3 and cs[0].time == datetime.fromtimestamp(ts0, timezone.utc)
        cov = coverage_report(db, NOW, "ZEDX")
        assert cov["datasets"]["option_candles_1m"]["rows"] == 3 and cov["datasets"]["rolling_option_candles_1m"]["rows"] == 2
        assert "NOT usable as fixed-contract" in cov["datasets"]["rolling_option_candles_1m"]["label"]
        assert cov["backtest_mode"] == "unavailable" and any("futures" in b for b in cov["blockers"])        # rolling + option candles alone never enable a backtest
        di.store_futures_candles(db, contract_id(world, "9100"), dd.parse_candles(payload(3, ts0=ts0)), NOW)
        db.add(OptionQuote(contract_id=ocid, quote_time=NOW, ingested_at=NOW, bid=D(1), ask=D(2), delta=D("0.5"), greeks_source="provider", source="t"))
        db.commit()
        assert coverage_report(db, NOW, "ZEDX")["backtest_mode"] == "quote_accurate"
        assert coverage_report(db, NOW, "OTHERX")["backtest_mode"] == "unavailable"                          # nothing about ZEDX is assumed for another underlying


def test_registered_futures_instrument_feeds_the_existing_loaders_and_feed_identity(world):
    cid = contract_id(world, "9100")
    ts0 = int((NOW - timedelta(hours=1)).timestamp()) // 60 * 60
    with Session(world) as db:
        inst = di.register_futures_instrument(db, cid)
        again = di.register_futures_instrument(db, cid)
        assert inst.id == again.id and inst.asset_class == "future" and inst.expiry == date(2026, 11, 26) and inst.multiplier == D("55")
        from app.models import InstrumentProviderIdentifier
        ident = db.scalar(select(InstrumentProviderIdentifier).where(InstrumentProviderIdentifier.instrument_id == inst.id))
        assert (ident.provider, ident.provider_instrument_id, ident.is_verified) == ("dhan", "9100|NSE_FNO", True)         # the shape the existing live-feed hub subscribes with
        di.store_futures_candles(db, cid, dd.parse_candles(payload(4, ts0=ts0)), NOW)
        db.commit()
        from app.services.intraday_runner import load_bars
        f = load_bars(db, inst.id, None, None)
        assert len(f) == 4 and f["time"].iloc[0] == datetime.fromtimestamp(ts0, timezone.utc) and float(f["volume"].iloc[0]) == 10.0
        assert di.futures_contract_for_instrument(db, inst.id).security_id == "9100"
        assert db.scalar(select(func.count()).select_from(Instrument).where(Instrument.asset_class == "future")) == 1


# ───────────── API ─────────────
def test_derivatives_api_auth_universe_contracts_coverage_and_jobs(client, engine, monkeypatch):
    assert client.get("/api/v1/derivatives/universe").status_code == 401
    login(client, engine)
    refresh(engine, base_master(), NOW)
    caps = client.get("/api/v1/derivatives/capabilities").json()
    assert caps["limits"]["rolling_max_span_days"] == 1 and caps["capabilities"]["futures_expired"]["status"] == "unavailable" and caps["capabilities"]["historical_bid_ask"]["status"] == "unavailable"
    uni = client.get("/api/v1/derivatives/universe").json()
    assert {(u["exchange"], u["category"]) for u in uni["underlyings"]} >= {("NSE", "index"), ("NSE", "stock"), ("BSE", "index"), ("MCX", "commodity"), ("NSE", "currency")}
    assert uni["master"]["derivative_rows"] == 12
    ul = client.get("/api/v1/derivatives/underlyings?exchange=NSE&category=stock").json()
    assert ul["total"] == 1 and ul["items"][0]["symbol"] == "ACME" and ul["items"][0]["cash_security_id"] == "2001"
    fut_id = contract_id(engine, "9100")
    with Session(engine) as db:
        rows = dd.parse_candles(payload(2, ts0=int((NOW - timedelta(minutes=5)).timestamp())))
        di.store_futures_candles(db, fut_id, rows, NOW)
        db.commit()
    fut_page = client.get("/api/v1/derivatives/contracts?kind=futures&underlying=ZEDX").json()
    covered = next(i for i in fut_page["items"] if i["security_id"] == "9100")["coverage"]
    assert covered["candles_1m"] == 2 and covered["first"].replace("+00:00", "") == rows[0].ts.isoformat().replace("+00:00", "") and covered["last"].replace("+00:00", "") == rows[-1].ts.isoformat().replace("+00:00", "")   # sqlite drops the tz suffix; Postgres keeps it
    assert covered["provider"] == "dhan" and covered["status"] == "available" and covered["missing_intervals"] is not None
    c = client.get("/api/v1/derivatives/contracts?kind=options&underlying=ZEDX&option_type=CE").json()
    assert c["total"] == 2 and {i["strike"] for i in c["items"]} == {"1000.0000", "1030.0000"} and all(i["lot_size"] == "40" for i in c["items"])
    assert client.get("/api/v1/derivatives/contracts?kind=futures&exchange=MCX").json()["items"][0]["trading_symbol"] == "GLD-Dec-FUT"
    assert client.get("/api/v1/derivatives/contracts?kind=options&expiry=2026-12-05&exchange=MCX").json()["total"] == 1
    assert client.post("/api/v1/derivatives/coverage/refresh", json={}).status_code == 403
    assert client.post("/api/v1/derivatives/coverage/refresh", json={}, headers=h(client)).json()["rows"] > 20
    cov = client.get("/api/v1/derivatives/coverage?exchange=NSE&category=index").json()
    assert cov["rows"] and all(r["exchange"] == "NSE" and r["category"] == "index" for r in cov["rows"]) and {r["data_type"] for r in cov["rows"]} >= {"futures_candles_1m", "option_chain_snapshots_live"}
    assert all(r["status"] in ("available", "blocked", "empty") for r in cov["rows"])
    started = []
    monkeypatch.setattr("app.api.derivatives.start_job_thread", lambda job_id, user_id: started.append(job_id))
    body = {"kind": "futures", "exchange": "NSE", "segment": "NSE_FNO", "days": 89}
    dry = client.post("/api/v1/derivatives/jobs", json=body, headers=h(client)).json()
    assert dry["dry_run"] is True and dry["requests"] == 5 and dry["by_kind"] == {"futures_intraday": 3, "futures_daily": 2} and not started        # estimate only: nothing requested
    real = client.post("/api/v1/derivatives/jobs", json={**body, "dry_run": False}, headers=h(client)).json()
    assert real["job"]["status"] == "queued" and real["job"]["tasks_total"] == 5 and started == [UUID(real["job"]["id"])]
    detail = client.get(f"/api/v1/derivatives/jobs/{real['job']['id']}").json()
    assert detail["task_counts"] == {"pending": 5} and detail["progress_pct"] == 0.0
    assert client.post(f"/api/v1/derivatives/jobs/{real['job']['id']}/cancel", json={}, headers=h(client)).json() == {"cancel_requested": True}
    assert client.get(f"/api/v1/derivatives/jobs/{real['job']['id']}").json()["status"] == "cancelled"
    assert client.post(f"/api/v1/derivatives/jobs/{real['job']['id']}/cancel", json={}, headers=h(client)).status_code == 409
    empty = client.post("/api/v1/derivatives/jobs", json={"kind": "option_candles", "underlying": "ZEDX", "dry_run": False}, headers=h(client))
    assert empty.status_code == 422 and "chain snapshot" in empty.text
    assert client.post("/api/v1/derivatives/jobs", json={"kind": "bogus"}, headers=h(client)).status_code == 422
    assert len(client.get("/api/v1/derivatives/jobs").json()) == 1
    fut = client.get("/api/v1/derivatives/contracts?kind=futures&underlying=ACME").json()["items"][0]["id"]
    reg = client.post(f"/api/v1/derivatives/futures/{fut}/register-instrument", json={}, headers=h(client)).json()
    assert reg["symbol"] == "ACME-Nov2026-FUT" and client.post(f"/api/v1/derivatives/futures/{fut}/register-instrument", json={}, headers=h(client)).json()["instrument_id"] == reg["instrument_id"]


def test_missing_sessions_are_visible_in_the_coverage_estimate(world):
    cid = contract_id(world, "9100")
    day1 = int(datetime(2026, 11, 3, 3, 45, tzinfo=timezone.utc).timestamp())
    with Session(world) as db:
        rows = dd.parse_candles(payload(120, ts0=day1))                                                  # 120 of the 375 expected minutes on one session
        di.store_futures_candles(db, cid, rows, NOW)
        db.commit()
        di.refresh_coverage(db, NOW)
        from app.models import DerivativeDataCoverage
        r = db.scalar(select(DerivativeDataCoverage).where(DerivativeDataCoverage.scope_key == "fut1m:NSE:NSE_FNO:FUTIDX"))
        assert r.sessions == 1 and r.missing_intervals["present"] == 120 and r.missing_intervals["expected_total"] == 375
