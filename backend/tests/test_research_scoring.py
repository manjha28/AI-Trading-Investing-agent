from datetime import date, datetime, timedelta, timezone

import json

from fastapi.encoders import jsonable_encoder

from app.services import research_scoring as rs

NOW = datetime(2026, 11, 5, 4, 0, tzinfo=timezone.utc)  # Thursday 09:30 IST


def fv(value):
    return {"value": None if value is None else str(value), "field_path": "x"}


def period(year, *, kind="annual", currency="INR", stale=False, **values):
    names = ("revenue", "net_income", "eps", "operating_margin", "total_assets", "total_liabilities",
             "total_equity", "operating_cash_flow", "free_cash_flow", "total_debt", "shares_outstanding")
    return {"period_type": kind, "period_end": f"{year}-03-31", "currency": currency, "stale": stale,
            "values": {n: fv(values.get(n)) for n in names}}


def healthy_periods():
    return [
        period(2024, revenue=800, net_income=80, total_equity=500, total_debt=100, total_assets=1000, total_liabilities=500,
               operating_cash_flow=90, free_cash_flow=50, operating_margin=15, shares_outstanding=100),
        period(2025, revenue=1000, net_income=130, total_equity=600, total_debt=100, total_assets=1100, total_liabilities=500,
               operating_cash_flow=140, free_cash_flow=90, operating_margin=18, shares_outstanding=100),
    ]


def trend_period(year, kind="annual", **growth):
    return {"period_type": kind, "period_end": f"{year}-03-31", "currency": "INR",
            "values": {k: {"value": "1", "year_on_year_growth_pct": None if g is None else str(g), "field_paths": []}
                       for k, g in growth.items()}}


def tech(status="available", **overrides):
    ind = {n: {"value": str(v), "bars_used": 250, "bars_required": 1} for n, v in
           dict(sma_50=95, sma_200=90, ema_20=100, ema_50=97, ema_200=92, rsi_14=58, distance_to_52w_high=-5, realized_volatility_20d=25).items()}
    base = {"status": status, "message": None, "latest_close": "100", "bars_available": 250, "indicators": ind}
    base.update(overrides)
    return base


def event(instrument_id, score, *, sentiment="positive", publisher="A", contradictory=False, days_old=1):
    return {"event_key": f"k{score}{publisher}{days_old}", "sentiment": sentiment, "score": score, "confidence_score": 80,
            "event_weight": 0.5, "contradictory_sources": contradictory,
            "items": [{"instrument_id": instrument_id, "publisher": publisher, "source": "google_news",
                       "published_at": (NOW - timedelta(days=days_old)).isoformat()}]}


def sections(*, periods=None, trend=None, technicals=None, sentiment=None, sector="Technology"):
    return {
        "fundamentals": {"status": "available", "periods": healthy_periods() if periods is None else periods,
                         "snapshot": {"values": {"sector": fv(sector), "industry": fv("Software")}}},
        "financials_trend": trend if trend is not None else {"status": "available", "periods": [
            trend_period(2025, revenue=25, net_income=62, eps=60, free_cash_flow=80),
            trend_period(2024, revenue=10, net_income=15, eps=14, free_cash_flow=20)]},
        "technicals": technicals if technicals is not None else tech(),
        "news_sentiment": sentiment,
    }


def good_sentiment(iid="I1"):
    return {"status": "available", "classifier_version": "v", "events": [
        event(iid, 0.5, publisher="A"), event(iid, 0.4, publisher="B"), event(iid, 0.3, publisher="C", days_old=2)]}


def test_fundamental_scores_from_ratios_with_provenance_of_basis():
    result = rs.score_fundamental(sections()["fundamentals"], "Technology", "Software")
    assert result["status"] == "scored" and 0 <= result["score"] <= 100
    keys = {m["key"] for m in result["factors"]}
    assert {"net_margin", "roe", "debt_to_equity", "cash_conversion", "share_change"} <= keys
    assert any("Interest coverage" in m for m in result["missing"])


def test_missing_inputs_are_blocked_not_zero_or_neutral():
    result = rs.score_fundamental({"periods": [period(2025, revenue=100)]}, "Technology", None)
    assert result["status"] == "blocked" and result["score"] is None and result["confidence"] is None
    assert result["blockers"]


def test_mixed_currency_blocks_fundamentals_without_fx_conversion():
    periods = healthy_periods()
    periods[0]["currency"] = "USD"
    result = rs.score_fundamental({"periods": periods}, None, None)
    assert result["status"] == "blocked" and "mixed currencies" in result["blockers"][0]


def test_implausible_operating_margin_unit_is_excluded():
    periods = healthy_periods()
    periods[1]["values"]["operating_margin"] = fv(1800)
    result = rs.score_fundamental({"periods": periods}, None, None)
    assert "operating_margin" not in {m["key"] for m in result["factors"]}
    assert any("unit error" in m for m in result["missing"])


def test_bank_excludes_leverage_and_caps_confidence():
    result = rs.score_fundamental(sections(sector="Financial Services")["fundamentals"], "Banks - Regional", None)
    keys = {m["key"] for m in result["factors"]}
    assert "debt_to_equity" not in keys and "cash_conversion" not in keys and "roe" in keys
    assert result["confidence"] <= 50


def test_negative_equity_raises_balance_sheet_flag():
    periods = healthy_periods()
    periods[1]["values"]["total_equity"] = fv(-10)
    result = rs.score_fundamental({"periods": periods}, None, None)
    assert any("equity" in f for f in result["risk_flags"])


def test_financial_trends_score_and_non_positive_base_excluded():
    result = rs.score_financial_trends(sections()["financials_trend"], sections()["fundamentals"])
    assert result["status"] == "scored" and result["score"] > 60
    none = rs.score_financial_trends({"status": "available", "periods": [trend_period(2025, revenue=None, net_income=None)]}, {"periods": []})
    assert none["status"] == "blocked"


def test_implausible_growth_is_excluded_and_flagged():
    trend = {"status": "available", "periods": [trend_period(2025, revenue=5000, net_income=20, eps=15, free_cash_flow=10)]}
    result = rs.score_financial_trends(trend, {"periods": []})
    assert "revenue_growth" not in {m["key"] for m in result["factors"]}
    assert any("implausible" in m for m in result["missing"])


def test_conflicting_annual_and_quarterly_growth_reduces_confidence():
    trend = {"status": "available", "periods": [
        trend_period(2025, "annual", revenue=20, net_income=20, eps=20, free_cash_flow=20),
        trend_period(2025, "quarterly", revenue=-10, net_income=-10, eps=-10, free_cash_flow=-10)]}
    result = rs.score_financial_trends(trend, {"periods": []})
    assert result["conflicts"] and result["status"] == "scored"


def test_stale_technicals_never_produce_a_score():
    result = rs.score_technical(tech("stale", staleness={"reason": "Latest bar 2026-11-03; expected 2026-11-04."}))
    assert result["status"] == "blocked" and result["score"] is None
    assert "2026-11-03" in result["blockers"][0]


def test_technical_scores_when_available():
    result = rs.score_technical(tech())
    assert result["status"] == "scored" and result["score"] > 60


def test_company_news_only_and_market_wide_excluded():
    sentiment = good_sentiment()
    sentiment["events"] += [event(None, -0.9, sentiment="negative", publisher="M") for _ in range(10)]
    result = rs.score_news_sentiment(sentiment, "I1", NOW)
    assert result["status"] == "scored" and result["score"] > 60
    assert "10 market/sector/macro" in result["notes"][0]


def test_low_company_coverage_is_blocked_not_neutral():
    sentiment = {"status": "available", "events": [event("I1", 0.5), event(None, 0.5), event(None, 0.5), event(None, 0.5)]}
    result = rs.score_news_sentiment(sentiment, "I1", NOW)
    assert result["status"] == "blocked" and result["score"] is None


def test_duplicate_publishers_do_not_inflate_confidence():
    one = {"status": "available", "classifier_version": "v", "events": [event("I1", 0.4, publisher="A", days_old=d) for d in (1, 2, 3)]}
    many = good_sentiment()
    a = rs.score_news_sentiment(one, "I1", NOW)["confidence"]
    b = rs.score_news_sentiment(many, "I1", NOW)["confidence"]
    assert a < b


def test_contradictory_news_is_reported_and_lowers_confidence():
    s = good_sentiment()
    s["events"].append(event("I1", -0.6, sentiment="negative", publisher="D"))
    s["events"].append(event("I1", -0.6, sentiment="negative", publisher="E", days_old=3))
    result = rs.score_news_sentiment(s, "I1", NOW)
    assert result["conflicts"]


def test_horizon_weights_sum_to_one_and_are_stored():
    for weights in rs.HORIZON_WEIGHTS.values():
        assert abs(sum(weights.values()) - 1) < 1e-9
    assessment = rs.build_assessment(sections(sentiment=good_sentiment()), instrument_id="I1", now=NOW)
    assert assessment["weights"] == rs.HORIZON_WEIGHTS
    assert assessment["model_version"] == rs.MODEL_VERSION
    assert assessment["validation"]["status"] == "provisional" and assessment["validation"]["backtested"] is False


def test_horizon_calculation_and_intraday_blocker():
    a = rs.build_assessment(sections(sentiment=good_sentiment()), instrument_id="I1", now=NOW)
    comps = a["components"]
    swing = a["horizons"]["swing"]
    expected = sum(rs.HORIZON_WEIGHTS["swing"][k] * comps[k]["score"] for k in comps)
    assert abs(swing["score"] - expected) < 0.11
    assert a["horizons"]["intraday"]["recommendation"] == "INSUFFICIENT_EVIDENCE"
    assert "intraday" in a["horizons"]["intraday"]["blockers"][0].lower()


def test_stale_technicals_block_swing_and_downgrade_long_term_buy_to_wait():
    secs = sections(sentiment=good_sentiment(), technicals=tech("stale", staleness={"reason": "stale"}))
    a = rs.build_assessment(secs, instrument_id="I1", now=NOW)
    assert a["horizons"]["swing"]["recommendation"] == "INSUFFICIENT_EVIDENCE"
    long_term = a["horizons"]["long_term"]
    assert long_term["score"] is not None
    assert long_term["raw_recommendation"] == "BUY" and long_term["recommendation"] == "WAIT"
    assert long_term["gates"][0]["code"] == "price_not_current"


def test_unscored_components_are_not_filled_with_neutral():
    secs = sections(sentiment={"status": "no_news_found", "events": []}, technicals=tech())
    a = rs.build_assessment(secs, instrument_id="I1", now=NOW)
    swing = a["horizons"]["swing"]
    assert "news_sentiment" in swing["components_missing"]
    assert all(c["component"] != "news_sentiment" for c in swing["contributions"])
    assert abs(sum(c["effective_weight"] for c in swing["contributions"]) - 1) < 0.01


def test_insufficient_scored_weight_blocks_horizon():
    secs = sections(periods=[], trend={"status": "unavailable", "periods": []}, technicals=tech(), sentiment=None)
    a = rs.build_assessment(secs, instrument_id="I1", now=NOW)
    assert a["horizons"]["positional"]["recommendation"] == "INSUFFICIENT_EVIDENCE"
    assert a["horizons"]["long_term"]["recommendation"] == "INSUFFICIENT_EVIDENCE"


def test_assessment_json_serializes_and_round_trips():
    a = rs.build_assessment(sections(sentiment=good_sentiment()), instrument_id="I1", now=NOW)
    assert json.loads(json.dumps(jsonable_encoder(a)))["model_version"] == rs.MODEL_VERSION


def test_expected_session_skips_weekend_and_holiday():
    # Monday 2026-11-02 morning IST -> last completed session is Friday 2026-10-30.
    monday = datetime(2026, 11, 2, 4, 0, tzinfo=timezone.utc)
    assert rs.expected_last_completed_session(monday)[0] == date(2026, 10, 30)
    # Thursday morning -> Wednesday; Thursday after close + grace -> Thursday.
    assert rs.expected_last_completed_session(NOW)[0] == date(2026, 11, 4)
    evening = datetime(2026, 11, 5, 13, 0, tzinfo=timezone.utc)  # 18:30 IST
    assert rs.expected_last_completed_session(evening)[0] == date(2026, 11, 5)


def test_staleness_detects_missing_session_and_avoids_false_weekend_stale():
    bar_nov3 = datetime(2026, 11, 3, tzinfo=timezone.utc)
    stale = rs.daily_bar_staleness(bar_nov3, NOW, country="IN", continuous=False, age_limit_seconds=172800)
    assert stale["stale"] and stale["expected_session"] == "2026-11-04" and "2026-11-03" in stale["reason"]
    friday_bar = datetime(2026, 10, 30, tzinfo=timezone.utc)
    monday_noon = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)
    ok = rs.daily_bar_staleness(friday_bar, monday_noon, country="IN", continuous=False, age_limit_seconds=172800)
    assert not ok["stale"] and ok["basis"] == "nse_session_calendar"


def test_non_indian_or_continuous_market_uses_age_limit():
    result = rs.daily_bar_staleness(datetime(2026, 11, 1, tzinfo=timezone.utc), NOW, country="US", continuous=False, age_limit_seconds=172800)
    assert result["basis"] == "age_limit" and result["stale"]
