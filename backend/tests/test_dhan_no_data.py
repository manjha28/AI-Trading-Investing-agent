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
