"""AI analyst: evidence pack, validation, caching, failures, and score separation.

The Gemini HTTP call is always replaced with a fake: these tests never reach the network."""
import copy
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import AiStockAnalysis, InstrumentResearchBrief, InstrumentResearchRun, User
from app.services import ai_analyst as ai
from app.services import research_scoring as rs
from app.services import valuation as val
from app.services import valuation_scoring as vs
from test_research_pipeline import client, csrf, get_instrument_id, register, test_engine  # noqa: F401  (fixtures)

NOW = datetime(2026, 11, 5, 5, 0, tzinfo=timezone.utc)
INSTRUMENT = SimpleNamespace(id=uuid4(), symbol="EASEMYTRIP", display_name="Easy Trip Planners", venue="NSE", currency="INR")


def fv(value):
    return {"value": None if value is None else str(value), "field_path": "x"}


def period(year, **values):
    names = ("revenue", "net_income", "eps", "operating_margin", "total_assets", "total_liabilities",
             "total_equity", "operating_cash_flow", "free_cash_flow", "total_debt", "shares_outstanding")
    return {"period_type": "annual", "period_end": f"{year}-03-31", "currency": "INR", "source": "yfinance",
            "fetched_at": "2026-11-01T00:00:00+00:00", "stale": False, "values": {n: fv(values.get(n)) for n in names}}


def sections(*, technical="available", with_fundamentals=True, with_news=True):
    periods = [
        period(2024, revenue=1400, net_income=260, eps=1.5, total_equity=1300, total_debt=20, total_assets=1600,
               total_liabilities=300, operating_cash_flow=230, free_cash_flow=200, shares_outstanding=1730),
        period(2025, revenue=1800, net_income=380, eps=1.9, total_equity=1650, total_debt=25, total_assets=2000,
               total_liabilities=350, operating_cash_flow=300, free_cash_flow=300, shares_outstanding=1740),
    ] if with_fundamentals else []
    events = [{
        "event_key": f"k{i}", "sentiment": "positive", "score": 0.4, "confidence_score": 80, "event_weight": 0.5,
        "impact": "medium", "event_category": "results", "contradictory_sources": False,
        "items": [{"instrument_id": str(INSTRUMENT.id), "headline": f"<b>Headline {i}</b>", "publisher": f"P{i}",
                   "source": "google_news", "published_at": "2026-11-04T05:00:00+00:00"}],
    } for i in range(3)] if with_news else []
    secs = {
        "fundamentals": {"status": "available" if periods else "unavailable", "periods": periods,
                         "snapshot": {"status": "available", "source": "yfinance", "fetched_at": "2026-11-01", "stale": False,
                                      "values": {"sector": fv("Consumer Cyclical"), "pe": fv("30")} if periods else {"sector": fv(None), "pe": fv(None)}}},
        "financials_trend": {"status": "available" if periods else "unavailable", "periods": [
            {"period_type": "annual", "period_end": "2025-03-31", "currency": "INR",
             "values": {"revenue": {"value": "1800", "year_on_year_growth_pct": "28.6", "field_paths": []}}}] if periods else []},
        "technicals": {"status": technical, "message": None, "source": "dhan", "latest_bar": "2026-11-04T00:00:00+00:00",
                       "latest_close": "100", "bars_available": 250, "staleness": None,
                       "indicators": {"rsi_14": {"value": "58", "bars_used": 250, "bars_required": 15},
                                      "sma_50": {"value": "95", "bars_used": 250, "bars_required": 50},
                                      "sma_200": {"value": "90", "bars_used": 250, "bars_required": 200},
                                      "ema_20": {"value": "100", "bars_used": 250, "bars_required": 20},
                                      "ema_50": {"value": "97", "bars_used": 250, "bars_required": 50},
                                      "ema_200": {"value": "92", "bars_used": 250, "bars_required": 200},
                                      "distance_to_52w_high": {"value": "-5", "bars_used": 250, "bars_required": 253}}},
        "news": {"status": "available" if with_news else "unavailable", "message": None, "items": [], "sources": []},
        "news_sentiment": {"status": "available" if events else "no_news_found", "classifier_version": "v", "events": events,
                           "freshness": {"latest_published_at": "2026-11-04"}},
        "data_quality": {"status": "issues", "items": [{"kind": "unavailable", "reason": "Interest expense not stored.", "field_path": "x"}]},
        "disclaimer": "Decision support, not investment advice.",
    }
    secs["assessment"] = rs.build_assessment(secs, instrument_id=str(INSTRUMENT.id), now=NOW)
    return secs


def pack_for(secs, horizon="swing"):
    return ai.build_evidence_pack(secs, instrument=INSTRUMENT, horizon=horizon, brief_created_at=NOW, now=NOW)


def good_raw(**over):
    raw = {
        "score": 68, "recommendation": "HOLD", "confidence": 62, "summary": "Profitable, low debt.",
        "reasoning": "Margins and cash conversion are healthy per the pack.", "strengths": ["Low total debt"],
        "weaknesses": ["Thin evidence of news diversity"], "risks": ["Single-sector exposure"],
        "bull_case": {"thesis": "Growth persists", "drivers": ["Revenue growth 28.6"], "invalidated_by": ["Margin compression"]},
        "bear_case": {"thesis": "Growth slows", "drivers": ["Valuation"], "invalidated_by": ["Sustained growth"]},
        "data_gaps": ["Interest expense not stored"], "disagreement_explanation": "Broadly agrees with the algorithm.",
        "valuation_view": {"label": "insufficient_evidence", "summary": "No comparable peers, so no verdict.", "supporting_metrics": ["P/B 2.0x (calculated)"],
                           "provider_vs_calculated": [], "limitations": ["No comparable peer universe."], "effect_on_assessment": "Valuation is excluded from the weights."},
    }
    raw.update(over)
    return raw


def gemini_body(raw, finish="STOP", block=None, thought=False):
    parts = ([{"text": "internal reasoning, must be ignored", "thought": True}] if thought else []) + [{"text": json.dumps(raw)}]
    body = {"candidates": [{"finishReason": finish, "content": {"role": "model", "parts": parts}}],
            "usageMetadata": {"promptTokenCount": 3000, "candidatesTokenCount": 600, "thoughtsTokenCount": 200, "totalTokenCount": 3800}}
    if block:
        body["promptFeedback"] = {"blockReason": block}
    return body


# ───────────── evidence pack ─────────────
def test_pack_contains_every_snapshot_area_and_the_algorithmic_reference_for_the_horizon():
    pack = pack_for(sections(), "positional")
    for key in ("fundamentals", "financial_trends", "technicals", "news", "news_sentiment", "freshness",
                "data_quality_issues", "algorithmic_assessment", "instrument", "requested_horizon"):
        assert key in pack
    assert pack["fundamentals"]["annual_periods"][0]["period_end"] == "2025-03-31"
    assert pack["fundamentals"]["annual_periods"][0]["values"]["revenue"] == "1800"
    assert pack["news_sentiment"]["company_event_count"] == 3
    assert pack["news_sentiment"]["company_events"][0]["headline"] == "Headline 0"  # markup stripped
    algo = pack["algorithmic_assessment"]["horizon"]
    assert algo["score"] == rs.build_assessment(sections(), instrument_id=str(INSTRUMENT.id), now=NOW)["horizons"]["positional"]["score"]
    assert "Interest expense not stored." in pack["data_quality_issues"][0]


def test_pack_never_contains_secrets_or_the_api_key(monkeypatch):
    monkeypatch.setattr(settings, "gemini_api_key", SecretStr("sk-secret-value"))
    assert "sk-secret-value" not in json.dumps(pack_for(sections()), default=str)


def test_snapshot_hash_ignores_request_time_but_tracks_content():
    base = sections()
    a = ai.snapshot_hash(pack_for(base))
    later = ai.build_evidence_pack(base, instrument=INSTRUMENT, horizon="swing", brief_created_at=NOW,
                                   now=NOW.replace(hour=9))
    assert ai.snapshot_hash(later) == a
    changed = copy.deepcopy(base)
    changed["technicals"]["latest_close"] = "101"
    assert ai.snapshot_hash(pack_for(changed)) != a


# ───────────── validation and guardrails ─────────────
def test_valid_result_is_normalised_and_stale_technicals_are_acknowledged_and_downgrade_buy():
    pack = pack_for(sections(technical="stale"))
    result = ai.normalize_result(good_raw(recommendation="BUY", score=80), pack)
    assert result["recommendation"] == "WAIT" and result["guardrails_applied"]
    assert any("Technical data is stale" in gap for gap in result["data_gaps"])


@pytest.mark.parametrize("bad", [{"score": 101}, {"score": -1}, {"score": 55.5}, {"score": True},
                                 {"confidence": 120}, {"recommendation": "STRONG_BUY"}, {"score": None, "recommendation": "BUY"}])
def test_out_of_range_or_malformed_output_is_rejected_not_repaired(bad):
    with pytest.raises(ai.AiAnalysisError) as error:
        ai.normalize_result(good_raw(**bad), pack_for(sections()))
    assert error.value.code == "ai_invalid_response"


def test_insufficient_evidence_may_have_a_null_score():
    result = ai.normalize_result(good_raw(score=None, recommendation="INSUFFICIENT_EVIDENCE", confidence=10), pack_for(sections()))
    assert result["score"] is None


def test_missing_data_is_acknowledged_even_if_the_model_forgets():
    pack = pack_for(sections(with_fundamentals=False, with_news=False))
    result = ai.normalize_result(good_raw(data_gaps=[]), pack)
    joined = " ".join(result["data_gaps"])
    assert "No fundamentals are stored." in joined and "No company-specific news" in joined


def test_no_usable_evidence_skips_the_api():
    secs = sections(technical="unavailable", with_fundamentals=False, with_news=False)
    assert ai.has_usable_evidence(pack_for(secs)) is False
    assert ai.local_insufficient_result(pack_for(secs))["recommendation"] == "INSUFFICIENT_EVIDENCE"


def test_comparison_is_deterministic_and_does_not_touch_algorithmic_figures():
    pack = pack_for(sections())
    algo_before = copy.deepcopy(pack["algorithmic_assessment"])
    algo_score = pack["algorithmic_assessment"]["horizon"]["score"]
    ai_score = int(algo_score) - 30 if algo_score >= 50 else int(algo_score) + 30
    result = ai.normalize_result(good_raw(score=ai_score), pack)
    comparison = ai.compare_with_algorithm(result, pack)
    assert comparison["level"] == "material"
    assert comparison["score_gap"] == round(ai_score - algo_score, 1)
    assert comparison["algorithmic_score"] == algo_score and comparison["ai_score"] == ai_score
    assert pack["algorithmic_assessment"] == algo_before  # untouched
    near = ai.compare_with_algorithm(ai.normalize_result(good_raw(score=int(round(algo_score))), pack), pack)
    assert near["level"] == "aligned"
    blocked = pack_for(sections(technical="stale"), "swing")  # swing requires technicals, so it is blocked
    assert ai.compare_with_algorithm(ai.normalize_result(good_raw(), blocked), blocked)["level"] == "not_comparable"


def test_analyse_passes_the_configured_model_key_and_parsed_usage(monkeypatch):
    monkeypatch.setattr(settings, "gemini_model", "test-model")
    seen = {}

    def fake_call(model, payload, api_key, timeout):
        seen.update(model=model, payload=payload, api_key=api_key)
        return gemini_body(good_raw())

    monkeypatch.setattr(ai, "_call_gemini", fake_call)
    result, usage = ai.analyse(pack_for(sections()), "swing", "sk-test")
    assert seen["api_key"] == "sk-test" and "sk-test" not in json.dumps(seen["payload"])
    assert seen["model"] == "test-model"
    assert usage == {"prompt_tokens": 3000, "completion_tokens": 600, "thinking_tokens": 200, "total_tokens": 3800}
    assert result["score"] == 68


@pytest.mark.parametrize("body,code", [
    (gemini_body(good_raw(), finish="MAX_TOKENS"), "ai_truncated"),
    (gemini_body(good_raw(), finish="SAFETY"), "ai_refused"),
    (gemini_body(good_raw(), block="PROHIBITED_CONTENT"), "ai_refused"),
    ({"candidates": []}, "ai_invalid_response"),
    ({"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "not json"}]}}]}, "ai_invalid_response"),
    ({"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "[1, 2]"}]}}]}, "ai_invalid_response"),
    ({"candidates": [{"finishReason": "STOP"}]}, "ai_invalid_response"),
])
def test_provider_response_problems_become_typed_errors(monkeypatch, body, code):
    monkeypatch.setattr(ai, "_call_gemini", lambda *_a: body)
    with pytest.raises(ai.AiAnalysisError) as error:
        ai.analyse(pack_for(sections()), "swing", "sk-test")
    assert error.value.code == code


def fake_response(status, error=None):
    class Response:
        status_code = status
        text = "echo sk-secret"

        def json(self):
            if error is None:
                return {}
            return {"error": error}

    return Response()


@pytest.mark.parametrize("status,error,code,http,needle", [
    (400, {"status": "INVALID_ARGUMENT", "details": [{"reason": "API_KEY_INVALID"}]}, "ai_auth_failed", 502, "GEMINI_API_KEY"),
    (403, {"status": "PERMISSION_DENIED"}, "ai_auth_failed", 502, "GEMINI_API_KEY"),
    (401, None, "ai_auth_failed", 502, "GEMINI_API_KEY"),
    (404, {"status": "NOT_FOUND", "message": "models/x is not found"}, "ai_model_unavailable", 502, "GEMINI_MODEL"),
    (429, {"status": "RESOURCE_EXHAUSTED"}, "ai_rate_limited", 429, "quota"),
    (503, {"status": "UNAVAILABLE", "message": "This model is currently experiencing high demand."}, "ai_provider_overloaded", 503, "HTTP 503"),
    (500, {"status": "INTERNAL"}, "ai_provider_internal_error", 502, "HTTP 500"),
    (504, {"status": "DEADLINE_EXCEEDED"}, "ai_provider_timeout", 504, "HTTP 504"),
    (400, {"status": "FAILED_PRECONDITION"}, "ai_provider_precondition", 502, "region or billing"),
    (400, {"status": "INVALID_ARGUMENT", "message": "bad schema"}, "ai_request_rejected", 502, "request format"),
    (502, None, "ai_provider_error", 502, "HTTP 502"),
])
def test_each_gemini_http_failure_gets_its_own_accurate_actionable_error(monkeypatch, status, error, code, http, needle):
    monkeypatch.setattr(ai.httpx, "post", lambda *a, **k: fake_response(status, error))
    with pytest.raises(ai.AiAnalysisError) as raised:
        ai._call_gemini("m", {}, "sk-secret", 5)
    assert raised.value.code == code and raised.value.status_code == http and needle in raised.value.message
    assert "sk-secret" not in raised.value.message and "models/x" not in raised.value.message and "bad schema" not in raised.value.message
    assert "temporarily unavailable" not in raised.value.message or code == "ai_provider_overloaded"  # unrelated failures are never mislabelled


def test_only_a_503_overload_is_labelled_overloaded_and_it_says_what_it_is():
    assert "overloaded" in ai.AiAnalysisError("ai_provider_overloaded", "Gemini is overloaded or temporarily unavailable (HTTP 503). Try again shortly.", 503).message


def test_every_call_logs_sanitized_status_category_and_duration_without_the_key(monkeypatch, caplog):
    caplog.set_level("INFO", logger="app.services.ai_analyst")
    secret = "AIza" + "x" * 35
    monkeypatch.setattr(ai.httpx, "post", lambda *a, **k: fake_response(
        503, {"status": "UNAVAILABLE", "message": f"high demand, key {secret} leaked?"}))
    with pytest.raises(ai.AiAnalysisError):
        ai._call_gemini("gemini-test", {"contents": "hello"}, secret, 5)
    line = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("gemini_call"))
    for expected in ("model=gemini-test", "outcome=http_error", "http_status=503", "gemini_status=UNAVAILABLE", "duration_ms=", "request_bytes="):
        assert expected in line
    assert secret not in line and "[REDACTED]" in line  # even a key echoed inside Gemini's own message is scrubbed
    caplog.clear()
    monkeypatch.setattr(ai.httpx, "post", lambda *a, **k: type("R", (), {"status_code": 200, "json": lambda self: {"ok": 1}})())
    ai._call_gemini("gemini-test", {}, secret, 5)
    assert "outcome=ok" in next(r.getMessage() for r in caplog.records if r.getMessage().startswith("gemini_call"))


def test_overload_is_retried_with_backoff_then_succeeds_and_nothing_else_is_retried(monkeypatch):
    sleeps, calls = [], []
    monkeypatch.setattr(ai, "_sleep", sleeps.append)
    monkeypatch.setattr(settings, "gemini_model", "test-model")
    outcomes = [ai.AiAnalysisError("ai_provider_overloaded", "x", 503), ai.AiAnalysisError("ai_provider_overloaded", "x", 503), gemini_body(good_raw())]

    def fake(*_a):
        calls.append(1)
        item = outcomes.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(ai, "_call_gemini", fake)
    result, _usage = ai.analyse(pack_for(sections()), "swing", "k")
    assert result["score"] == 68 and len(calls) == 3 and len(sleeps) == 2 and 2.0 <= sleeps[0] <= 3.0 and 5.0 <= sleeps[1] <= 6.0


@pytest.mark.parametrize("code", ["ai_auth_failed", "ai_model_unavailable", "ai_rate_limited", "ai_timeout", "ai_request_rejected",
                                  "ai_provider_internal_error", "ai_unreachable"])
def test_non_overload_errors_are_never_retried(monkeypatch, code):
    calls = []
    monkeypatch.setattr(ai, "_sleep", lambda _s: pytest.fail("must not back off"))
    monkeypatch.setattr(settings, "gemini_model", "test-model")
    monkeypatch.setattr(ai, "_call_gemini", lambda *_a: calls.append(1) or (_ for _ in ()).throw(ai.AiAnalysisError(code, "m", 502)))
    with pytest.raises(ai.AiAnalysisError) as error:
        ai.analyse(pack_for(sections()), "swing", "k")
    assert error.value.code == code and len(calls) == 1


def test_persistent_overload_gives_up_after_bounded_retries_with_the_real_reason(monkeypatch):
    calls = []
    monkeypatch.setattr(ai, "_sleep", lambda _s: None)
    monkeypatch.setattr(settings, "gemini_model", "test-model")
    monkeypatch.setattr(ai, "_call_gemini", lambda *_a: calls.append(1) or (_ for _ in ()).throw(ai.AiAnalysisError("ai_provider_overloaded", "m", 503)))
    with pytest.raises(ai.AiAnalysisError) as error:
        ai.analyse(pack_for(sections()), "swing", "k")
    assert error.value.code == "ai_provider_overloaded" and len(calls) == ai.OVERLOAD_RETRIES + 1


def test_network_failure_and_backend_timeout_are_distinguished_from_gemini_errors(monkeypatch, caplog):
    caplog.set_level("INFO", logger="app.services.ai_analyst")

    def unreachable(*_a, **_k):
        raise ai.httpx.ConnectError("dns")

    monkeypatch.setattr(ai.httpx, "post", unreachable)
    with pytest.raises(ai.AiAnalysisError) as error:
        ai._call_gemini("m", {}, "k", 5)
    assert error.value.code == "ai_unreachable" and "could not reach" in error.value.message
    assert any("outcome=network_error" in r.getMessage() for r in caplog.records)
    monkeypatch.setattr(ai.httpx, "post", lambda *a, **k: (_ for _ in ()).throw(ai.httpx.ReadTimeout("slow")))
    with pytest.raises(ai.AiAnalysisError) as error:
        ai._call_gemini("m", {}, "k", 90)
    assert error.value.code == "ai_timeout" and "GEMINI_TIMEOUT_SECONDS" in error.value.message and "90" in error.value.message
    assert any("outcome=backend_timeout" in r.getMessage() and "configured_timeout_s=90" in r.getMessage() for r in caplog.records)


def test_gemini_request_format_url_header_schema_and_key_placement(monkeypatch):
    seen = {}

    def fake_post(url, json=None, headers=None, timeout=None, trust_env=None):
        seen.update(url=url, body=json, headers=headers, timeout=timeout)
        return type("R", (), {"status_code": 200, "json": lambda self: gemini_body(good_raw())})()

    monkeypatch.setattr(ai.httpx, "post", fake_post)
    ai._call_gemini("gemini-test", ai.build_request(pack_for(sections())), "sk-secret-key", 12)
    assert seen["url"] == "https://generativelanguage.googleapis.com/v1beta/models/gemini-test:generateContent"
    assert seen["headers"]["x-goog-api-key"] == "sk-secret-key" and seen["timeout"] == 12
    assert "sk-secret-key" not in seen["url"] and "sk-secret-key" not in json.dumps(seen["body"])
    body = seen["body"]
    assert body["systemInstruction"]["parts"][0]["text"] == ai.SYSTEM_PROMPT
    assert body["contents"][0]["role"] == "user" and "Evidence pack" in body["contents"][0]["parts"][0]["text"]
    cfg = body["generationConfig"]
    assert cfg["responseMimeType"] == "application/json" and cfg["maxOutputTokens"] == ai.MAX_OUTPUT_TOKENS
    schema = cfg["responseSchema"]
    text = json.dumps(schema)
    assert "additionalProperties" not in text and '"minimum"' not in text and '"maximum"' not in text
    assert schema["properties"]["score"] == {"type": "integer", "nullable": True}
    assert set(schema["required"]) == set(ai.RESPONSE_SCHEMA["required"])  # every field of the stored schema is requested
    assert schema["properties"]["recommendation"]["enum"] == list(ai.RECOMMENDATIONS)


def test_thought_parts_are_ignored_when_parsing_and_usage_is_tracked():
    raw, usage = ai._parse_result(gemini_body(good_raw(), thought=True))
    assert raw["score"] == 68 and usage["thinking_tokens"] == 200 and usage["total_tokens"] == 3800
    assert ai._parse_result({"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": json.dumps(good_raw())}]}}]})[1] is None


@pytest.mark.parametrize("name,expected", [
    ("gemini-2.5-flash", "gemini-2.5-flash"), ("models/gemini-2.5-pro", "gemini-2.5-pro"), ("  gemini-x  ", "gemini-x"),
    ("../etc/passwd", None), ("gemini?key=abc", None), ("", None), ("a" * 60, None), ("gem ini", None),
])
def test_model_name_is_validated_before_it_reaches_the_url(monkeypatch, name, expected):
    monkeypatch.setattr(settings, "gemini_model", name)
    assert ai.configured_model() == expected


def test_invalid_model_name_blocks_the_call_and_reports_not_configured(client, test_engine, ai_on, monkeypatch):
    instrument_id, _b, _ = seed_brief(client, test_engine)
    monkeypatch.setattr(settings, "gemini_model", "bad/model")
    monkeypatch.setattr(ai, "_call_gemini", lambda *_a: pytest.fail("must not call Gemini"))
    assert post(client, instrument_id).json()["detail"]["code"] == "ai_not_configured"


def test_timeout_maps_to_504(monkeypatch):
    def boom(*_a, **_k):
        raise ai.httpx.ReadTimeout("slow")

    monkeypatch.setattr(ai.httpx, "post", boom)
    with pytest.raises(ai.AiAnalysisError) as error:
        ai._call_gemini("m", {}, "k", 5)
    assert error.value.code == "ai_timeout" and error.value.status_code == 504


# ───────────── API: cache, separation, failures, auth ─────────────
@pytest.fixture
def ai_on(monkeypatch):
    monkeypatch.setattr(settings, "ai_enabled", True)
    monkeypatch.setattr(settings, "gemini_api_key", SecretStr("sk-test-key"))
    monkeypatch.setattr(settings, "gemini_model", "test-model")


def seed_brief(client, test_engine, secs=None):
    register(client, test_engine, f"ai-{uuid4().hex[:6]}@example.com")
    instrument_id = get_instrument_id(client)
    with Session(test_engine) as db:
        user = db.scalar(select(User).order_by(User.created_at.desc()))
        run = InstrumentResearchRun(
            instrument_id=UUID(instrument_id), user_id=user.id, started_at=NOW, finished_at=NOW, status="complete",
            sources_attempted=0, sources_succeeded=0, failures=[], progress=[], cancel_requested=False,
        )
        db.add(run)
        db.flush()
        secs = secs or sections()
        # make the pack's company events match this instrument
        for event in secs["news_sentiment"]["events"]:
            for item in event["items"]:
                item["instrument_id"] = instrument_id
        secs["assessment"] = rs.build_assessment(secs, instrument_id=instrument_id, now=NOW)
        brief = InstrumentResearchBrief(run_id=run.id, instrument_id=UUID(instrument_id), sections=secs,
                                        renderer="template", data_quality=[], created_at=NOW)
        db.add(brief)
        db.commit()
        return instrument_id, str(brief.id), copy.deepcopy(secs)


def post(client, instrument_id, horizon="swing"):
    return client.post(f"/api/v1/research/instruments/{instrument_id}/ai-analysis", json={"horizon": horizon}, headers=csrf(client))


def test_disabled_or_unconfigured_returns_a_clear_error_and_never_calls_gemini(client, test_engine, monkeypatch):
    instrument_id, _brief, _ = seed_brief(client, test_engine)
    monkeypatch.setattr(settings, "ai_enabled", False)  # independent of the developer's real .env
    monkeypatch.setattr(ai, "_call_gemini", lambda *_a: pytest.fail("must not call Gemini"))
    assert post(client, instrument_id).json()["detail"]["code"] == "ai_disabled"
    monkeypatch.setattr(settings, "ai_enabled", True)
    monkeypatch.setattr(settings, "gemini_api_key", None)
    assert post(client, instrument_id).json()["detail"]["code"] == "ai_not_configured"
    status = client.get(f"/api/v1/research/instruments/{instrument_id}/ai-analysis").json()
    assert status["enabled"] is True and status["configured"] is False and status["analyses"] == {}


def test_requires_a_stored_snapshot_and_csrf(client, test_engine, ai_on):
    register(client, test_engine, "ai-nosnap@example.com")
    instrument_id = get_instrument_id(client)
    assert post(client, instrument_id).json()["detail"]["code"] == "no_research_snapshot"
    assert client.post(f"/api/v1/research/instruments/{instrument_id}/ai-analysis", json={"horizon": "swing"}).status_code == 403
    assert post(client, instrument_id, "monthly").status_code == 422


def test_successful_analysis_is_stored_separately_with_model_horizon_and_snapshot_and_cached(client, test_engine, ai_on, monkeypatch):
    instrument_id, brief_id, secs_before = seed_brief(client, test_engine)
    calls = []

    def fake(model, payload, api_key, timeout):
        calls.append((model, payload))
        return gemini_body(good_raw())

    monkeypatch.setattr(ai, "_call_gemini", fake)
    first = post(client, instrument_id, "swing")
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["cached"] is False and body["horizon"] == "swing" and body["model"] == "test-model"
    assert body["brief_id"] == brief_id and len(body["snapshot_hash"]) == 64 and body["created_at"]
    assert body["result"]["score"] == 68 and body["comparison"]["ai_score"] == 68
    assert body["comparison"]["algorithmic_score"] == secs_before["assessment"]["horizons"]["swing"]["score"]
    # repeat: served from cache, no second API call, same row
    second = post(client, instrument_id, "swing").json()
    assert second["cached"] is True and second["id"] == body["id"] and len(calls) == 1
    # a different horizon is a different cache entry
    assert post(client, instrument_id, "long_term").json()["cached"] is False and len(calls) == 2
    # GET never calls Gemini and returns both
    listing = client.get(f"/api/v1/research/instruments/{instrument_id}/ai-analysis").json()
    assert set(listing["analyses"]) == {"swing", "long_term"} and len(calls) == 2
    # score separation: the stored algorithmic assessment is byte-for-byte unchanged
    with Session(test_engine) as db:
        brief = db.scalar(select(InstrumentResearchBrief))
        assert brief.sections["assessment"] == secs_before["assessment"]
        assert brief.renderer == "template"
        assert db.scalar(select(func.count()).select_from(AiStockAnalysis)) == 2


def test_new_research_snapshot_does_not_reuse_an_old_analysis(client, test_engine, ai_on, monkeypatch):
    instrument_id, _brief, secs = seed_brief(client, test_engine)
    calls = []
    monkeypatch.setattr(ai, "_call_gemini", lambda *_a: calls.append(1) or gemini_body(good_raw()))
    post(client, instrument_id)
    with Session(test_engine) as db:
        old = db.scalar(select(InstrumentResearchBrief))
        run = InstrumentResearchRun(instrument_id=old.instrument_id, user_id=db.get(InstrumentResearchRun, old.run_id).user_id,
                                    started_at=datetime(2026, 11, 6, tzinfo=timezone.utc), finished_at=NOW, status="complete",
                                    sources_attempted=0, sources_succeeded=0, failures=[], progress=[], cancel_requested=False)
        db.add(run)
        db.flush()
        db.add(InstrumentResearchBrief(run_id=run.id, instrument_id=old.instrument_id, sections=secs, renderer="template",
                                       data_quality=[], created_at=datetime(2026, 11, 6, tzinfo=timezone.utc)))
        db.commit()
    assert post(client, instrument_id).json()["cached"] is False and len(calls) == 2


@pytest.mark.parametrize("failure,code,http", [
    (ai.AiAnalysisError("ai_rate_limited", "quota", 429), "ai_rate_limited", 429),
    (ai.AiAnalysisError("ai_timeout", "slow", 504), "ai_timeout", 504),
    (ai.AiAnalysisError("ai_auth_failed", "bad key", 502), "ai_auth_failed", 502),
])
def test_api_failures_are_reported_and_not_cached_so_a_retry_can_succeed(client, test_engine, ai_on, monkeypatch, failure, code, http):
    instrument_id, _b, _ = seed_brief(client, test_engine)

    def failing(*_a):
        raise failure

    monkeypatch.setattr(ai, "_call_gemini", failing)
    response = post(client, instrument_id)
    assert response.status_code == http and response.json()["detail"]["code"] == code
    with Session(test_engine) as db:
        assert db.scalar(select(func.count()).select_from(AiStockAnalysis)) == 0
    monkeypatch.setattr(ai, "_call_gemini", lambda *_a: gemini_body(good_raw()))
    assert post(client, instrument_id).status_code == 200


def test_invalid_model_output_is_not_stored(client, test_engine, ai_on, monkeypatch):
    instrument_id, _b, _ = seed_brief(client, test_engine)
    monkeypatch.setattr(ai, "_call_gemini", lambda *_a: gemini_body(good_raw(score=250)))
    assert post(client, instrument_id).json()["detail"]["code"] == "ai_invalid_response"
    with Session(test_engine) as db:
        assert db.scalar(select(func.count()).select_from(AiStockAnalysis)) == 0


def test_empty_snapshot_returns_insufficient_evidence_without_an_api_call(client, test_engine, ai_on, monkeypatch):
    secs = sections(technical="unavailable", with_fundamentals=False, with_news=False)
    instrument_id, _b, _ = seed_brief(client, test_engine, secs)
    monkeypatch.setattr(ai, "_call_gemini", lambda *_a: pytest.fail("no evidence: must not call Gemini"))
    body = post(client, instrument_id).json()
    assert body["source"] == "local_gate" and body["result"]["recommendation"] == "INSUFFICIENT_EVIDENCE"
    assert body["result"]["score"] is None and body["usage"] is None


def test_other_users_cannot_read_or_trigger_analysis_on_someone_elses_snapshot(client, test_engine, ai_on, monkeypatch):
    instrument_id, _b, _ = seed_brief(client, test_engine)
    monkeypatch.setattr(ai, "_call_gemini", lambda *_a: gemini_body(good_raw()))
    assert post(client, instrument_id).status_code == 200
    client.cookies.clear()
    register(client, test_engine, "ai-other@example.com")
    assert client.get(f"/api/v1/research/instruments/{instrument_id}/ai-analysis").json()["analyses"] == {}
    assert post(client, instrument_id).json()["detail"]["code"] == "no_research_snapshot"




def test_status_and_results_identify_gemini_and_never_expose_the_key(client, test_engine, ai_on, monkeypatch):
    instrument_id, _b, _ = seed_brief(client, test_engine)
    monkeypatch.setattr(ai, "_call_gemini", lambda *_a: gemini_body(good_raw()))
    status = client.get(f"/api/v1/research/instruments/{instrument_id}/ai-analysis").json()
    assert status["provider"] == "gemini" and status["model"] == "test-model"
    body = post(client, instrument_id).json()
    assert body["provider"] == "gemini" and body["model"] == "test-model" and body["source"] == "gemini"
    assert body["prompt_version"] == ai.PROMPT_VERSION
    assert "sk-test-key" not in json.dumps(status) + json.dumps(body, default=str)


def test_changing_the_model_or_provider_never_reuses_a_cached_result(client, test_engine, ai_on, monkeypatch):
    instrument_id, _b, _ = seed_brief(client, test_engine)
    calls = []
    monkeypatch.setattr(ai, "_call_gemini", lambda model, *_a: calls.append(model) or gemini_body(good_raw()))
    assert post(client, instrument_id).json()["cached"] is False
    assert post(client, instrument_id).json()["cached"] is True
    monkeypatch.setattr(settings, "gemini_model", "another-model")
    other = post(client, instrument_id).json()
    assert other["cached"] is False and other["model"] == "another-model" and calls == ["test-model", "another-model"]
    # GET shows only the currently configured model's result
    assert client.get(f"/api/v1/research/instruments/{instrument_id}/ai-analysis").json()["analyses"]["swing"]["model"] == "another-model"
    # a row written by the previous provider/prompt generation is invisible to the new cache key
    with Session(test_engine) as db:
        row = db.scalar(select(AiStockAnalysis).where(AiStockAnalysis.model == "gemini:test-model"))
        assert row is not None
        row.model, row.prompt_version = "gpt-4o-mini", "ai-analyst-v1"
        db.commit()
    monkeypatch.setattr(settings, "gemini_model", "test-model")
    assert post(client, instrument_id).json()["cached"] is False
    monkeypatch.setattr(ai, "PROMPT_VERSION", "ai-analyst-v3")
    assert post(client, instrument_id).json()["cached"] is False


def test_ai_analysis_accepts_only_the_three_research_horizons_and_hides_legacy_intraday_rows(client, test_engine, ai_on, monkeypatch):
    instrument_id, brief_id, _ = seed_brief(client, test_engine)
    monkeypatch.setattr(ai, "_call_gemini", lambda *_a: gemini_body(good_raw()))
    for horizon in ("intraday", "monthly", ""):
        assert post(client, instrument_id, horizon).status_code == 422
    for horizon in ("swing", "positional", "long_term"):
        assert post(client, instrument_id, horizon).status_code == 200
    with pytest.raises(KeyError):
        ai.build_evidence_pack(sections(), instrument=INSTRUMENT, horizon="intraday", brief_created_at=NOW, now=NOW)
    # a pre-existing intraday analysis stays in the table untouched but is no longer served
    with Session(test_engine) as db:
        template = db.scalar(select(AiStockAnalysis))
        db.add(AiStockAnalysis(user_id=template.user_id, brief_id=template.brief_id, instrument_id=template.instrument_id, horizon="intraday",
                               model=template.model, prompt_version=template.prompt_version, snapshot_hash="x" * 64, source="gemini",
                               result=template.result, usage=None, created_at=NOW))
        db.commit()
    listing = client.get(f"/api/v1/research/instruments/{instrument_id}/ai-analysis").json()
    assert set(listing["analyses"]) == {"swing", "positional", "long_term"}
    with Session(test_engine) as db:
        assert db.scalar(select(func.count()).select_from(AiStockAnalysis).where(AiStockAnalysis.horizon == "intraday")) == 1


# ───────────── valuation evidence in the Gemini payload (milestone: shared evidence layer) ─────────────
def scored_valuation_section():
    """Valuation evidence with enough current, same-basis, peer-compared metrics in two independent groups to be scored."""
    def metric(key, label, origin, provider=None):
        return {"key": key, "label": label, "status": "reported" if origin == "provider_reported" else "calculated", "value": "10.00", "unit": "x", "origin": origin,
                "provider": provider, "reporting_period": "2026-09-30", "retrieved_at": NOW.isoformat(), "freshness": "current", "statement_basis": "consolidated",
                "reason": None, "warnings": [], "cross_check": None}
    metrics = {"pe_ttm": metric("pe_ttm", "P/E", "calculated"), "pb": metric("pb", "P/B", "provider_reported", "yfinance"), "price_to_sales": metric("price_to_sales", "Price / Sales", "calculated")}
    peers = {"status": "compared", "peer_group": "industry: Software", "metrics": {k: {"status": "compared", "peer_count": 12, "percentile_rank": "20", "origin": m["origin"]} for k, m in metrics.items()}}
    valuation = {"model_version": val.MODEL_VERSION, "currency": "INR", "as_of": NOW.isoformat(), "price": {"value": "100", "current": True, "as_of": "2026-11-04", "problem": None},
                 "metrics": metrics, "peer_comparison": peers, "sector": "Technology", "industry": "Software", "data_gaps": []}
    valuation["scoring"] = vs.score_valuation(valuation, sector="Technology", industry="Software")
    return valuation


def with_valuation(secs, *, peers=None, scored=False):
    """Attach the shared valuation evidence (and its production scoring) exactly as the pipeline does, then rebuild the assessment."""
    fundamentals = copy.deepcopy(secs["fundamentals"])
    for p in fundamentals["periods"]:
        p.setdefault("statement_basis", None)
    snapshot_values = {"market_cap": {"value": "1000000000", "source": "yfinance", "fetched_at": NOW.isoformat()}, "pb": {"value": "2.5", "source": "yfinance", "fetched_at": NOW.isoformat()},
                       "sector": {"value": "Technology"}, "industry": {"value": "Software"}}
    fundamentals["snapshot"] = {"source": "yfinance", "fetched_at": NOW.isoformat(), "values": snapshot_values}
    secs = {**secs, "fundamentals": fundamentals}
    valuation = val.compute_valuation(currency="INR", technicals=secs["technicals"], fundamentals=fundamentals, now=NOW)
    valuation["peer_comparison"] = peers if peers is not None else {"status": "insufficient_peers", "metrics": {k: {"status": "insufficient_peers", "peer_count": 1, "reason": "only 1 comparable peer(s)"} for k in val.PEER_METRICS}}
    valuation["sector"], valuation["industry"] = "Technology", "Software"
    valuation["scoring"] = vs.score_valuation(valuation, sector="Technology", industry="Software")
    secs["valuation"] = scored_valuation_section() if scored else valuation
    secs["assessment"] = rs.build_assessment(secs, instrument_id=str(INSTRUMENT.id), now=NOW)
    return secs


def compared_peers(rank="20"):
    return {"status": "compared", "peer_group": "industry: Software", "metrics": {k: {"status": "compared", "peer_count": 12, "percentile_rank": rank, "origin": "provider_reported"} for k in val.PEER_METRICS}}


def test_pack_carries_the_exact_normalized_valuation_evidence_and_the_production_component():
    secs = with_valuation(sections())
    pack = pack_for(secs)
    evidence = pack["valuation"]
    assert evidence == val.normalized_evidence(secs["valuation"])  # byte-for-byte the shared evidence layer
    for key, m in secs["valuation"]["metrics"].items():
        assert evidence["metrics"][key]["value"] == m["value"] and evidence["metrics"][key]["origin"] == m["origin"] and evidence["metrics"][key]["status"] == m["status"]
    assert set(evidence["metrics"]) == {"market_cap", "enterprise_value", "pe_ttm", "pb", "ev_ebitda", "price_to_sales", "peg", "dividend_yield", "book_value_per_share",
                                        "net_debt_ebitda", "roe", "roce", "interest_coverage"}
    component = pack["valuation_component"]
    produced = secs["assessment"]["components"]["valuation"]
    assert component["status"] == produced["status"] and component["score"] == produced["score"] and component["blockers"] == produced["blockers"]
    assert component["weights_by_horizon"] == {h: rs.HORIZON_WEIGHTS[h]["valuation"] for h in rs.HORIZONS} and component["provisional"] is True
    assert component["valuation_model_version"] == vs.SCORING_VERSION and component["model_version"] == rs.MODEL_VERSION
    assert evidence["scoring"] == secs["valuation"]["scoring"] and "peer_comparison" in evidence


def test_pack_marks_missing_valuation_evidence_instead_of_inventing_it():
    pack = pack_for(with_valuation(sections()))
    metrics = pack["valuation"]["metrics"]
    for key in ("enterprise_value", "ev_ebitda", "net_debt_ebitda", "peg"):
        assert metrics[key]["status"] == "unavailable" and metrics[key]["value"] is None and metrics[key]["reason"]
    assert any("cash and equivalents" in gap for gap in pack["valuation"]["unavailable_or_unreliable"])
    assert pack["valuation_component"]["status"] == "blocked" and pack["valuation_component"]["score"] is None
    bare = pack_for(sections())  # a report whose valuation section is absent sends no valuation figures at all
    assert bare["valuation"] is None
    assert bare["valuation_component"]["status"] == "blocked" and bare["valuation_component"]["score"] is None


def test_pack_never_contains_dhan_audited_unreliable_fields():
    secs = with_valuation(sections())
    secs["fundamentals"]["snapshot"]["values"]["pe"] = {"value": "18.6", "source": "dhan", "fetched_at": NOW.isoformat()}
    secs = with_valuation(secs)
    text = json.dumps(pack_for(secs), default=str).lower()
    for banned in ("ebitda\": \"207", "pe_ratio", "industry_pb", "pe_to_sector", "roe\": \"18"):
        assert banned not in text
    assert val.compute_valuation(currency="INR", technicals=secs["technicals"], fundamentals=secs["fundamentals"], now=NOW)["metrics"]["pe_ttm"]["provider"] != "dhan"


def test_prompt_and_payload_changes_invalidate_cached_analyses():
    assert ai.PROMPT_VERSION == "ai-analyst-v3-valuation" and ai.PROMPT_VERSION not in {"ai-analyst-v1", "ai-analyst-v2-gemini"}
    assert "valuation_view" in ai.RESPONSE_SCHEMA["required"] and "VALUATION" in ai.SYSTEM_PROMPT
    request = ai.build_request(pack_for(with_valuation(sections())))
    assert "valuation_view" in request["generationConfig"]["responseSchema"]["required"]
    assert "valuation_component" in request["contents"][0]["parts"][0]["text"] and "\"valuation\"" in request["contents"][0]["parts"][0]["text"]


def test_cached_analysis_from_the_previous_prompt_version_is_never_served(client, test_engine, ai_on, monkeypatch):
    instrument_id, _brief, _ = seed_brief(client, test_engine)
    calls = []
    monkeypatch.setattr(ai, "_call_gemini", lambda *_a: calls.append(1) or gemini_body(good_raw()))
    assert post(client, instrument_id).json()["cached"] is False
    with Session(test_engine) as db:
        row = db.scalar(select(AiStockAnalysis))
        row.prompt_version = "ai-analyst-v2-gemini"  # an analysis produced before the valuation payload existed
        db.commit()
    again = post(client, instrument_id).json()
    assert again["cached"] is False and again["prompt_version"] == ai.PROMPT_VERSION and len(calls) == 2
    listing = client.get(f"/api/v1/research/instruments/{instrument_id}/ai-analysis").json()
    assert listing["analyses"]["swing"]["prompt_version"] == ai.PROMPT_VERSION


def test_valuation_verdict_requires_a_scored_production_component():
    blocked_pack = pack_for(with_valuation(sections()))
    raw = good_raw(valuation_view={**good_raw()["valuation_view"], "label": "expensive", "summary": "Looks dear."})
    result = ai.normalize_result(raw, blocked_pack)
    assert result["valuation_view"]["label"] == "insufficient_evidence" and result["valuation_view"]["model_label"] == "expensive"
    assert any("not scored" in g for g in result["guardrails_applied"])  # recorded, never silent
    scored_pack = pack_for(with_valuation(sections(), scored=True))
    assert scored_pack["valuation_component"]["status"] == "scored"
    kept = ai.normalize_result(raw, scored_pack)
    assert kept["valuation_view"]["label"] == "expensive" and "model_label" not in kept["valuation_view"]


@pytest.mark.parametrize("bad", [{"label": "cheap"}, {"label": None}])
def test_invalid_valuation_view_is_rejected_not_repaired(bad):
    with pytest.raises(ai.AiAnalysisError):
        ai.normalize_result(good_raw(valuation_view={**good_raw()["valuation_view"], **bad}), pack_for(sections()))
    broken = good_raw()
    del broken["valuation_view"]
    with pytest.raises(ai.AiAnalysisError):
        ai.normalize_result(broken, pack_for(sections()))


def test_the_ai_result_keeps_its_valuation_view_separate_from_the_algorithmic_score(client, test_engine, ai_on, monkeypatch):
    instrument_id, _brief, secs_before = seed_brief(client, test_engine)
    monkeypatch.setattr(ai, "_call_gemini", lambda *_a: gemini_body(good_raw()))
    body = post(client, instrument_id).json()
    assert body["result"]["valuation_view"]["label"] == "insufficient_evidence" and body["prompt_version"] == "ai-analyst-v3-valuation"
    with Session(test_engine) as db:
        assert db.scalar(select(InstrumentResearchBrief)).sections["assessment"] == secs_before["assessment"]  # the production result is untouched
