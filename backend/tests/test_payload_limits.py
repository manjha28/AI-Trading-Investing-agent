"""Page-load payloads stay bounded: thinned history, paginated corporate actions. Rows are TEST DATA."""
from datetime import date, datetime, timezone
from decimal import Decimal

from fastapi import Response
from sqlalchemy.orm import Session

from app.api.corporate_actions import list_corporate_actions
from app.api.portfolio_transactions import _thin
from app.models import CorporateAction, Instrument, User
from tests.test_crypto_delta import engine  # noqa: F401

NOW = datetime(2026, 11, 23, 12, tzinfo=timezone.utc)


def test_thin_keeps_at_most_n_points_evenly_and_always_the_newest():
    rows = list(range(10_000))
    out = _thin(rows, 600)
    assert len(out) == 600 and out[0] == 0 and out[-1] == 9_999 and out == sorted(out)
    assert _thin(rows[:50], 600) == rows[:50]                                  # a short series is returned whole


def test_corporate_actions_are_paged_newest_first_with_the_total_in_a_header(engine):  # noqa: F811
    with Session(engine) as db:
        user = User(email="ca@example.com", password_hash="x", email_verified=True, created_at=NOW)
        inst = Instrument(symbol="CAINST", display_name="CA Inst", asset_class="equity", venue="NSE", currency="INR", country="IN", timezone="Asia/Kolkata", created_at=NOW, is_synthetic=False)
        db.add_all([user, inst]); db.flush()
        for d in range(1, 8):
            db.add(CorporateAction(user_id=user.id, instrument_id=inst.id, action_type="dividend", ex_date=date(2026, 11, d), amount=Decimal("1.5"), source="test", created_at=NOW))
        db.commit()
        session = type("S", (), {"user_id": user.id})()
        response = Response()
        page = list_corporate_actions(response=response, instrument_id=None, limit=3, offset=0, active_session=session, db=db)
        assert len(page) == 3 and response.headers["x-total-count"] == "7"
        assert [a.ex_date.day for a in page] == [7, 6, 5]
        second = list_corporate_actions(response=Response(), instrument_id=None, limit=3, offset=3, active_session=session, db=db)
        assert [a.ex_date.day for a in second] == [4, 3, 2]


def test_compact_daily_report_drops_the_long_fields_and_keeps_what_the_list_shows():
    from app.api.research import _compact_report
    levels = {"status": "available", "entry_zone": {"low": "1", "high": "2"}, "stop": "0.5", "targets": [{"label": "T1", "price": "3"}], "explanation": "long text " * 50, "derivation": {"rule": "x" * 400},
              "support_levels": list(range(40)), "resistance_levels": list(range(40)), "pivots": {"daily": {"P": "1"}}}
    conf = {"score": 70, "band": "High", "parts": [{"key": "a"}] * 8, "setup": {"checks": [1] * 10}, "note": "n" * 300, "win_confidence": {"line": "Measured win rate 38%"}}
    row = {"instrument_id": "i1", "symbol": "AAA", "levels": {"positional": levels}, "horizons": {"positional": {"recommendation": "BUY", "buy_confidence": conf}}, "confidence": conf}
    out = _compact_report({"buckets": {"buy": [row]}, "counts": {"buy": 1}})["buckets"]["buy"][0]
    kept, original = out["levels"]["positional"], levels
    assert out["compact"] is True and kept["entry_zone"] == original["entry_zone"] and kept["stop"] == "0.5" and kept["targets"] == original["targets"]
    assert not {"explanation", "derivation", "support_levels", "resistance_levels", "pivots", "stop_reason"} & set(kept)
    assert out["confidence"]["score"] == 70 and out["confidence"]["win_confidence"]["line"].startswith("Measured") and not {"parts", "setup", "note"} & set(out["confidence"])
    assert "parts" not in out["horizons"]["positional"]["buy_confidence"] and row["confidence"]["parts"], "the stored full row is untouched"
