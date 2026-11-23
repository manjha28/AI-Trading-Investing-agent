"""Test data only: a hand-made httpx response stands in for Dhan's DH-907 reply."""
import httpx
import pytest

from app import broker_adapters
from app.broker_adapters import BrokerAdapterError, DhanNoData
from app.services import dhan_company_info


def _reply(monkeypatch, status, body):
    monkeypatch.setattr(httpx, "request", lambda method, url, **kw: httpx.Response(status, json=body, request=httpx.Request(method, url)))


def test_company_info_dh907_is_no_data_not_a_failure(monkeypatch, caplog):
    _reply(monkeypatch, 400, {"errorType": "Input_Exception", "errorCode": "DH-907", "errorMessage": "no data found"})
    with caplog.at_level("INFO"):
        with pytest.raises(DhanNoData):
            broker_adapters._request("POST", "https://api.dhan.co/v2/data/companyinfo", diagnostic_stage="company_info_api_request")
    assert not [r for r in caplog.records if r.levelname == "WARNING"]           # no more warning noise for an expected answer


def test_other_company_info_errors_are_still_failures(monkeypatch):
    _reply(monkeypatch, 400, {"errorCode": "DH-905", "errorMessage": "bad input"})
    with pytest.raises(BrokerAdapterError) as info:
        broker_adapters._request("POST", "https://api.dhan.co/v2/data/companyinfo", diagnostic_stage="company_info_api_request")
    assert not isinstance(info.value, DhanNoData)


def test_no_data_securities_are_remembered_for_a_week_then_retried():
    clock = {"t": 1000.0}
    dhan_company_info.mark_no_data("999001", now=lambda: clock["t"])
    assert dhan_company_info.has_no_data("999001", now=lambda: clock["t"])
    clock["t"] += dhan_company_info.NO_DATA_SECONDS + 1
    assert not dhan_company_info.has_no_data("999001", now=lambda: clock["t"])
    assert not dhan_company_info.has_no_data("never-marked")


def test_option_fills_with_different_symbol_text_still_pair_by_security_id():  # test data only
    from datetime import datetime, timezone
    from decimal import Decimal
    from uuid import uuid4

    from app.api.trades import _match_dhan_fills
    from app.models import DhanTradeMirror

    def fill(side, price, day, symbol):
        return DhanTradeMirror(id=uuid4(), user_id=uuid4(), source="dhan", trade_id=str(uuid4()), symbol=symbol, security_id="52123", exchange_segment="NSE_FNO", side=side,
                               quantity=Decimal("1500"), price=Decimal(price), charges=Decimal("20"), currency="INR", trade_at=datetime(2026, 11, day, 5, 0, tzinfo=timezone.utc))
    opening = fill("BUY", "3.10", 12, "ONGC-Nov2026-222-PE")          # trading-symbol style
    closing = fill("SELL", "4.25", 20, "ONGC 23 NOV 222 PUT")         # custom-symbol style
    result = _match_dhan_fills([opening, closing])
    assert len(result["matches"]) == 1 and result["matches"][0]["side"] == "long" and not result["open_fill_ids"]


def test_trade_book_fills_are_added_once_and_a_trade_book_failure_never_fails_the_sync():  # test data only
    from app.api.brokers import _merge_trade_book

    class Reader:
        def __init__(self, book=None, error=None):
            self.book, self.error = book or [], error

        def get_trade_book(self):
            if self.error:
                raise self.error
            return self.book
    history = [{"exchangeTradeId": "1", "securityId": "52123"}]
    sell = {"exchangeTradeId": "2", "securityId": "52123"}
    merged, note = _merge_trade_book(Reader([history[0], sell]), history)
    assert [r["exchangeTradeId"] for r in merged] == ["1", "2"] and "1 fill" in note       # the fill history already lists is not added twice
    merged, note = _merge_trade_book(Reader(error=RuntimeError("down")), history)
    assert merged == history and "could not be read" in note


def _jwt(exp: int) -> str:  # test data only
    import base64, json
    part = lambda obj: base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()    # noqa: E731
    return f"{part({'alg': 'none'})}.{part({'exp': exp})}.sig"


def test_stored_session_expiry_never_outlives_the_token_itself():
    from datetime import datetime, timedelta, timezone

    from app.broker_adapters import clamp_to_token_expiry, jwt_expiry
    exp = datetime(2026, 11, 21, 9, 51, tzinfo=timezone.utc)
    token = _jwt(int(exp.timestamp()))
    assert jwt_expiry(token) == exp
    assert clamp_to_token_expiry(token, exp + timedelta(hours=5, minutes=30)) == exp          # the 15:21 vs 09:51 mismatch that left the app "connected" with a dead token
    assert clamp_to_token_expiry(token, exp - timedelta(hours=1)) == exp - timedelta(hours=1)  # an earlier stored expiry is kept
    assert jwt_expiry("not-a-jwt") is None and clamp_to_token_expiry("not-a-jwt", exp) == exp
