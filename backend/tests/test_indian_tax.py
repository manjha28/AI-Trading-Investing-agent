"""Income-tax estimates, kept apart from transaction costs. Expected figures worked out by hand (FY2025-26 rules as configured)."""
from decimal import Decimal as D

import pytest

from app.services.indian_tax import RULES, TaxConfigError, estimate, slab_tax
from tests.test_signals_lab_api import API, client, engine, h, login  # noqa: F401


def t(r, k="total"):
    return D(r["tax_with_trading"][k])


def test_new_regime_slabs_and_incremental_tax_on_speculative_income():
    # other income 15,00,000: 0.05x4L + 0.10x4L + 0.15x3L = 20,000 + 40,000 + 45,000 = 1,05,000 -> with 4% cess 1,09,200
    # plus intraday 2,00,000 -> 17,00,000: + 0.15x1L + 0.20x1L... = 20k + 40k + 60k + 20k = 1,40,000 -> 1,45,600 with cess ; difference 36,400
    r = estimate("2025-26", "new", other_taxable_income=D(1500000), intraday_net=D(200000))
    assert slab_tax(D(1500000), RULES["2025-26"].regimes["new"]) == D(105000) and slab_tax(D(1700000), RULES["2025-26"].regimes["new"]) == D(140000)
    assert r["tax_without_trading"] == "109200" and t(r) == D(145600) and r["estimated_additional_tax"] == "36400"
    assert "speculative business income" in r["intraday_speculative"]["treatment"] and "informational" in r["label"]


def test_intraday_is_never_taxed_as_capital_gains():
    r = estimate("2025-26", "new", other_taxable_income=D(0), intraday_net=D(2000000))
    assert D(r["tax_with_trading"]["special_rate_tax"]) == 0 and D(r["tax_with_trading"]["slab_tax"]) > 0


def test_speculative_loss_is_not_set_off_against_other_income_and_is_carried_forward():
    r = estimate("2025-26", "new", other_taxable_income=D(1500000), intraday_net=D(-50000))
    assert r["estimated_additional_tax"] == "0" and r["tax_without_trading"] == "109200" and t(r) == D(109200)
    assert r["intraday_speculative"]["loss_to_carry_forward"] == "50000"
    bf = estimate("2025-26", "new", other_taxable_income=D(1500000), intraday_net=D(100000), brought_forward_speculative_loss=D(30000))
    assert bf["intraday_speculative"]["taxable_speculative_income"] == "70000"


def test_delivery_stcg_and_ltcg_use_special_rates_and_exemption():
    # STCG 1,00,000 x 20% = 20,000 ; LTCG (2,25,000 - 1,25,000) x 12.5% = 12,500 ; cess 4% -> 33,800
    r = estimate("2025-26", "new", other_taxable_income=D(0), intraday_net=D(0), delivery_stcg=D(100000), delivery_ltcg=D(225000))
    assert r["delivery_capital_gains"]["ltcg_taxable"] == "100000" and D(r["tax_with_trading"]["special_rate_tax"]) == D(32500) and t(r) == D(33800)
    below = estimate("2025-26", "new", other_taxable_income=D(0), intraday_net=D(0), delivery_ltcg=D(100000))
    assert t(below) == 0


def test_old_regime_and_rebates():
    # taxable 6,00,000 old regime: 0.05 x 2.5L + 0.20 x 1L = 12,500 + 20,000 = 32,500 ; cess -> 33,800
    assert estimate("2025-26", "old", other_taxable_income=D(600000), intraday_net=D(0))["tax_without_trading"] == "33800"
    assert estimate("2025-26", "old", other_taxable_income=D(500000), intraday_net=D(0))["tax_without_trading"] == "0"        # s.87A: tax 12,500 fully rebated
    assert estimate("2025-26", "new", other_taxable_income=D(1150000), intraday_net=D(0))["tax_without_trading"] == "0"       # new regime rebate up to 12,00,000
    # just above the threshold marginal relief caps tax at the excess: slab tax 61,500 -> 10,000 ; cess -> 10,400
    assert estimate("2025-26", "new", other_taxable_income=D(1210000), intraday_net=D(0))["tax_without_trading"] == "10400"


def test_surcharge_applies_above_50_lakh_and_cap_gains_surcharge_is_capped():
    base = estimate("2025-26", "new", other_taxable_income=D(6000000), intraday_net=D(0))
    slab = slab_tax(D(6000000), RULES["2025-26"].regimes["new"])           # 1,80,000 + 0.30 x 36,00,000 = 12,60,000? computed independently below
    assert slab == D(400000 * 0 + 20000 + 40000 + 60000 + 80000 + 100000 + 3600000 * D("0.30"))
    assert D(base["tax_with_trading"]["surcharge_rate"]) == D("0.10")
    assert D(base["tax_with_trading"]["total"]) == ((slab * D("1.10")) * D("1.04")).quantize(D(1))


def test_unconfigured_year_or_regime_is_an_error_and_unverified_years_are_flagged():
    with pytest.raises(TaxConfigError):
        estimate("2019-20", "new", other_taxable_income=D(0), intraday_net=D(0))
    with pytest.raises(TaxConfigError):
        estimate("2025-26", "hybrid", other_taxable_income=D(0), intraday_net=D(0))
    assert estimate("2026-27", "new", other_taxable_income=D(0), intraday_net=D(0))["rules"]["verified"] is False


def test_api_estimates_costs_and_tax_with_auth_csrf_and_validation(client, engine):
    assert client.post(f"/api/v1/costs/estimate", json={"executions": []}).status_code in (401, 403)
    login(client, engine)
    body = {"exchange": "NSE", "product": "intraday", "executions": [{"order_id": "a", "side": "buy", "quantity": "100", "price": "1000"}, {"order_id": "b", "side": "sell", "quantity": "100", "price": "1010"}]}
    assert client.post("/api/v1/costs/estimate", json=body).status_code == 403            # no CSRF
    r = client.post("/api/v1/costs/estimate", json=body, headers=h(client)).json()
    assert r["breakdown"]["total"] == "82.72" and r["breakdown"]["brokerage"] == "40.00" and r["schedule"]["exchange"] == "NSE"
    assert client.post("/api/v1/costs/estimate", json={**body, "exchange": "BSE"}, headers=h(client)).json()["breakdown"]["total"] == "84.33"
    assert client.post("/api/v1/costs/estimate", json={**body, "product": "delivery"}, headers=h(client)).json()["breakdown"]["total"] == "223.52"
    assert client.post("/api/v1/costs/estimate", json={**body, "product": "fno"}, headers=h(client)).status_code == 422
    bad = {**body, "executions": [{"order_id": "a", "side": "buy", "quantity": "0", "price": "1000"}]}
    assert client.post("/api/v1/costs/estimate", json=bad, headers=h(client)).status_code == 422
    tax = client.post("/api/v1/costs/tax-estimate", json={"financial_year": "2025-26", "other_taxable_income": "1500000", "intraday_net_after_charges": "200000"}, headers=h(client))
    assert tax.status_code == 200 and tax.json()["estimated_additional_tax"] == "36400" and "not a trading cost" in tax.json()["label"]
    assert client.post("/api/v1/costs/tax-estimate", json={"financial_year": "2019-20"}, headers=h(client)).status_code == 422
    sch = client.get("/api/v1/costs/schedules").json()
    assert {s["id"] for s in sch["schedules"]} >= {"dhan_nse_intraday_2026_11", "dhan_bse_intraday_2026_11", "dhan_nse_delivery_2026_11"} and all(s["verification"] for s in sch["schedules"])
