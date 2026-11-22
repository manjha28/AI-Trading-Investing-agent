"""Shadow follow-up of v2 ladders, the provisional blue-sky review, the v1-vs-v2 comparison and the hidden endpoint. Test data: hand-made bars/ladders in an in-memory database."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.main import app
from app.models import Instrument, MarketBar, User
from app.models.pipeline_v2 import PipelineV2Shadow
from app.services import pipeline_v2 as P
from app.services import shadow_outcomes as S

D0 = date(2026, 6, 1)
LV_NOW = {"entry_status": "ENTER_NOW", "planned_entry": "100.00", "stop": "95.00", "t1": "110.00"}
LV_WAIT = {"entry_status": "WAIT_FOR_ENTRY", "planned_entry": "100.00", "stop": "95.00", "t1": "110.00"}


def bars(*rows):                                      # (open, high, low, close) per session after the decision candle
    return [(D0 + timedelta(days=i), *r) for i, r in enumerate(rows)]


def test_enter_now_fills_at_the_next_open_and_t1_before_stop():
    r = S.simulate(LV_NOW, bars((101, 104, 100, 103), (103, 111, 102, 110)), "positional")
    assert r["outcome"] == "t1" and r["entry"] == 101 and r["exit"] == 110.0 and r["net_r"] > 0


def test_stop_is_checked_before_t1_on_the_same_bar():
    r = S.simulate(LV_NOW, bars((101, 112, 94, 100)), "swing")          # one bar reaches both the stop and T1: the stop wins
    assert r["outcome"] == "stop" and r["net_r"] < -0.9


def test_a_waiting_order_fills_only_at_its_planned_entry_and_the_fill_bar_checks_only_the_stop():
    assert S.simulate(LV_WAIT, bars((103, 104, 101, 102)) * 1, "swing")["outcome"] == "pending"                      # not filled yet, window still open
    r = S.simulate(LV_WAIT, bars((102, 103, 99.5, 101), (101, 111, 100, 110)), "swing")
    assert r["outcome"] == "t1" and r["entry"] == 100                                                                   # filled at the limit on bar 1, T1 on bar 2
    on_fill_bar = S.simulate(LV_WAIT, bars((102, 111, 99.5, 110)), "swing")                                             # T1 reached on the fill bar itself: not credited (order unknown)
    assert on_fill_bar["outcome"] == "pending"
    never = S.simulate(LV_WAIT, bars(*[(103, 104, 101, 102)] * 5), "swing")
    assert never["outcome"] == "unfilled"


def test_time_exit_after_the_horizon_window():
    flat = bars(*[(101, 103, 99, 102)] * 17)
    r = S.simulate(LV_NOW, flat, "swing")                                   # swing window is 15 sessions
    assert r["outcome"] == "time" and r["exit"] == 102


def test_statistics_helpers():
    lo, hi = S.wilson(34, 100)
    assert 0.25 < lo < 0.34 < hi < 0.44
    m, a, b = S.mean_ci([1.0, -1.0, 1.0, -1.0, 1.0, 1.0])
    assert a < m < b and S.mean_ci([1.0])[0] != S.mean_ci([1.0])[0]       # NaN with a single point
    st = S.summarise([{"outcome": "t1", "net_r": 1.0}, {"outcome": "stop", "net_r": -1.0}, {"outcome": "pending"}, {"outcome": "unfilled"}])
    assert st["resolved_trades"] == 2 and st["pending"] == 1 and st["unfilled"] == 1 and st["t1_before_stop"] == 0.5


def _stats(n, mean, lo, hi):
    return {"resolved_trades": n, "mean_net_r": mean, "net_r_ci95": [lo, hi]}


def test_the_two_month_review_rule():
    assert S.review_message(_stats(40, 0.3, 0.1, 0.5), 20)["due"] is False
    few = S.review_message(_stats(12, 0.3, -0.2, 0.8), 70)
    assert few["due"] is True and few["tell_owner"] is False and "only 12 resolved" in few["message"]
    bad = S.review_message(_stats(80, 0.12, -0.05, 0.29), 61)
    assert bad["tell_owner"] is True and "TELL THE OWNER" in bad["message"] and "includes zero" in bad["message"]
    ok = S.review_message(_stats(80, 0.28, 0.1, 0.46), 61)
    assert ok["tell_owner"] is False and "above zero" in ok["message"]


@pytest.fixture
def eng():
    e = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(e)
    yield e
    Base.metadata.drop_all(e)
    e.dispose()


def _setup(db):
    uid, other = uuid4(), uuid4()
    for u in (uid, other):
        db.add(User(id=u, email=f"{u}@example.invalid", password_hash="x", email_verified=True, created_at=datetime(2026, 1, 1, tzinfo=timezone.utc)))
    mk = lambda sym: Instrument(id=uuid4(), symbol=sym, display_name=sym, asset_class="equity", venue="NSE", currency="INR", country="IN", timezone="Asia/Kolkata", is_synthetic=False, relink_required=False,  # noqa: E731
                                created_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    a, b = mk("AAA"), mk("BBB")
    db.add_all([a, b])
    db.commit()
    return uid, other, a, b


def _bars(db, inst, rows):
    for i, (o, h, lo, c) in enumerate(rows):
        t = datetime.combine(D0 + timedelta(days=i), datetime.min.time(), tzinfo=timezone.utc)
        db.add(MarketBar(id=uuid4(), instrument_id=inst.id, interval="1d", timestamp=t, open=Decimal(o), high=Decimal(h), low=Decimal(lo), close=Decimal(c), volume=Decimal("1000"), source="dhan", retrieved_at=t, is_synthetic=False))
    db.commit()


def _shadow(db, uid, inst, v1, v2, *, blue=False, levels=LV_NOW, at=datetime(2026, 4, 1, tzinfo=timezone.utc), h="positional"):
    lv = {"entry_status": levels["entry_status"], "planned_entry": levels["planned_entry"], "stop": levels["stop"], "t1": levels["t1"], "t1_rr": 2.0, "ready_to_buy": True, "entry_label": "x"}
    db.add(PipelineV2Shadow(instrument_id=inst.id, user_id=uid, brief_id=None, computed_at=at, as_of_candle=D0 - timedelta(days=1), v2_buy_horizons=int(v2 == "BUY"), relaxation_dependent_ladders=int(blue and v2 == "BUY"),
                            payload={"version": P.VERSION, "horizons": {h: {"v1_recommendation": v1, "v2_recommendation": v2, "stage_failed": None if v2 == "BUY" else "timing", "reasons": ["why"],
                                                                         "levels": lv if v2 == "BUY" else None, "rank": {"grade": "A", "label": "High", "sort_score": 3090}, "blue_sky": {"near_high": blue, "relaxation_dependent": blue}}}}))
    db.commit()


def test_review_splits_blue_sky_ladders_from_the_rest_and_counts_shadow_days(eng):
    now = datetime(2026, 6, 20, tzinfo=timezone.utc)
    with Session(eng) as db:
        uid, _other, a, b = _setup(db)
        _bars(db, a, [("101", "104", "100", "103"), ("103", "111", "102", "110")])          # AAA: ENTER_NOW, T1 hit
        _bars(db, b, [("101", "103", "94", "96")])                                           # BBB: stopped out
        _shadow(db, uid, a, "WAIT", "BUY", blue=True)
        _shadow(db, uid, b, "WAIT", "BUY", blue=False)
        out = S.review(db, uid, now)
    assert out["shadow_days"] == 80
    assert out["groups"]["blue_sky_provisional"]["resolved_trades"] == 1 and out["groups"]["blue_sky_provisional"]["t1_before_stop"] == 1.0
    assert out["groups"]["other_v2_buys"]["resolved_trades"] == 1 and out["groups"]["other_v2_buys"]["t1_before_stop"] == 0.0
    assert out["review"]["due"] is True and out["review"]["tell_owner"] is False and "only 1 resolved" in out["review"]["message"]


def test_compare_shows_v1_vs_v2_and_is_owner_scoped(eng):
    with Session(eng) as db:
        uid, other, a, b = _setup(db)
        _shadow(db, uid, a, "BUY", "WAIT")                       # v1 said BUY, v2 says wait (timing)
        _shadow(db, uid, b, "WAIT", "BUY", blue=True)
        _shadow(db, other, a, "BUY", "BUY")                      # another owner's row must never appear
        out = P.compare(db, uid)
    assert out["stocks"] == 2 and out["transitions"]["positional"] == {"BUY -> WAIT": 1, "WAIT -> BUY": 1}
    assert {(c["symbol"], c["v1"], c["v2"]) for c in out["changed"]} == {("AAA", "BUY", "WAIT"), ("BBB", "WAIT", "BUY")} and out["changed_total"] == 2
    assert [r["symbol"] for r in out["v2_buy_cards"]] == ["BBB"] and out["relaxation_dependent_ladders"][0]["symbol"] == "BBB"


def test_the_endpoint_is_hidden_and_needs_a_verified_session(eng):
    def override():
        with Session(eng) as s:
            yield s
    app.dependency_overrides[get_db] = override
    try:
        assert "/api/v1/research/pipeline-v2/compare" not in app.openapi()["paths"]            # hidden from the API docs
        r = TestClient(app).get("/api/v1/research/pipeline-v2/compare")
        assert r.status_code in (401, 403)                                                        # anonymous: refused
    finally:
        app.dependency_overrides.pop(get_db, None)
