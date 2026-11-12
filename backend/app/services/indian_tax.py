"""Indian income-tax ESTIMATES for trading results. Informational only; never part of per-trade transaction costs or backtests.

Separate categories (never mixed):
- Equity INTRADAY (speculative business income, s.43(5)): taxed at slab rates as business income. Net speculative result is after brokerage and
  statutory charges (all deductible business expenses, including STT). A speculative LOSS can only be set off against speculative income
  (carried forward 4 years); it is never set off against salary/other income.
- Equity DELIVERY (capital gains): STCG (s.111A) and LTCG (s.112A, above an exemption) at special rates. Never taxed as intraday.
- F&O is a different (non-speculative business) category and is intentionally NOT modelled here.
Rates are configuration by financial year with an effective date and source. FY2026-27 is a CARRY-FORWARD of FY2025-26 and marked unverified.
Not modelled: marginal relief on surcharge, AMT, advance-tax interest, unused basic-exemption adjustment against special-rate gains, other heads.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

D = Decimal
RUPEE = D(1)
LAKH = D(100000)
CRORE = D(10000000)


@dataclass(frozen=True)
class Regime:
    slabs: tuple[tuple[Decimal | None, Decimal], ...]     # (upper bound of band or None, rate); bands ascending
    standard_deduction: Decimal
    rebate_income_limit: Decimal                          # taxable income up to this gets the s.87A rebate
    rebate_max: Decimal
    marginal_relief_above_rebate: bool
    surcharge: tuple[tuple[Decimal, Decimal], ...]        # (income above, rate) ascending


@dataclass(frozen=True)
class TaxRules:
    fy: str
    effective_from: str
    source: str
    verified: bool
    note: str
    regimes: dict[str, Regime]
    cess: Decimal
    stcg_111a: Decimal
    ltcg_112a: Decimal
    ltcg_exemption: Decimal
    cap_gains_surcharge_cap: Decimal


_NEW_2025 = Regime(slabs=((D(400000), D(0)), (D(800000), D("0.05")), (D(1200000), D("0.10")), (D(1600000), D("0.15")), (D(2000000), D("0.20")), (D(2400000), D("0.25")), (None, D("0.30"))),
                   standard_deduction=D(75000), rebate_income_limit=D(1200000), rebate_max=D(60000), marginal_relief_above_rebate=True,
                   surcharge=((D(5000000), D("0.10")), (D(10000000), D("0.15")), (D(20000000), D("0.25"))))
_OLD_2025 = Regime(slabs=((D(250000), D(0)), (D(500000), D("0.05")), (D(1000000), D("0.20")), (None, D("0.30"))),
                   standard_deduction=D(50000), rebate_income_limit=D(500000), rebate_max=D(12500), marginal_relief_above_rebate=False,
                   surcharge=((D(5000000), D("0.10")), (D(10000000), D("0.15")), (D(20000000), D("0.25")), (D(50000000), D("0.37"))))

RULES: dict[str, TaxRules] = {
    "2025-26": TaxRules("2025-26", "2025-04-01", "Finance Act 2025 / Union Budget 2025 (as understood by the implementer; confirm with a tax professional)", True,
                        "STCG 20% (s.111A), LTCG 12.5% above Rs 1.25 lakh (s.112A) for transfers on/after 23-Jul-2024.", {"new": _NEW_2025, "old": _OLD_2025},
                        D("0.04"), D("0.20"), D("0.125"), D("125000"), D("0.15")),
    "2026-27": TaxRules("2026-27", "2026-04-01", "CARRIED FORWARD from FY2025-26: not checked against the Finance Act 2026", False,
                        "Verify slabs, rebate and capital-gains rates for FY2026-27 before relying on this.", {"new": _NEW_2025, "old": _OLD_2025},
                        D("0.04"), D("0.20"), D("0.125"), D("125000"), D("0.15")),
}


class TaxConfigError(ValueError):
    pass


def _r(x: Decimal) -> Decimal:
    return x.quantize(RUPEE, rounding=ROUND_HALF_UP)


def slab_tax(taxable: Decimal, regime: Regime) -> Decimal:
    tax, lower = D(0), D(0)
    for upper, rate in regime.slabs:
        top = taxable if upper is None else min(taxable, upper)
        if top > lower:
            tax += (top - lower) * rate
        if upper is None or taxable <= upper:
            break
        lower = upper
    return tax


def _surcharge_rate(income: Decimal, regime: Regime) -> Decimal:
    rate = D(0)
    for above, r in regime.surcharge:
        if income > above:
            rate = r
    return rate


def _tax(rules: TaxRules, regime_name: str, normal_income: Decimal, stcg: Decimal, ltcg_taxable: Decimal) -> dict[str, Decimal]:
    regime = rules.regimes[regime_name]
    normal = max(D(0), normal_income)    # deductions (incl. standard deduction) are applied by the caller before this point
    slab = slab_tax(normal, regime)
    if normal <= regime.rebate_income_limit:
        slab = max(D(0), slab - min(slab, regime.rebate_max))
    elif regime.marginal_relief_above_rebate:
        slab = min(slab, normal - regime.rebate_income_limit)     # marginal relief just above the rebate threshold
    special = stcg * rules.stcg_111a + ltcg_taxable * rules.ltcg_112a
    total_income = normal + stcg + ltcg_taxable
    sr = _surcharge_rate(total_income, regime)
    surcharge = slab * sr + special * min(sr, rules.cap_gains_surcharge_cap)
    base = slab + special
    cess = (base + surcharge) * rules.cess
    return {"slab_tax": slab, "special_rate_tax": special, "surcharge": surcharge, "cess": cess, "total": base + surcharge + cess, "surcharge_rate": sr}


def estimate(fy: str, regime: str, *, other_taxable_income: Decimal, intraday_net: Decimal, brought_forward_speculative_loss: Decimal = D(0),
             delivery_stcg: Decimal = D(0), delivery_ltcg: Decimal = D(0)) -> dict[str, Any]:
    """Estimated incremental income tax caused by trading results. ``intraday_net`` is ALREADY after brokerage and statutory charges.
    ``other_taxable_income`` is taxable income from other heads AFTER the standard deduction/other deductions."""
    rules = RULES.get(fy)
    if rules is None:
        raise TaxConfigError(f"no tax rules configured for FY {fy}; configured: {', '.join(RULES)}")
    if regime not in rules.regimes:
        raise TaxConfigError(f"unknown regime {regime!r}; choose new or old")
    other = max(D(0), other_taxable_income)
    # speculative business income: losses (this year and brought forward) only offset speculative income
    spec_after = intraday_net - brought_forward_speculative_loss
    spec_income = max(D(0), spec_after)
    carry_forward = max(D(0), -spec_after)
    stcg = max(D(0), delivery_stcg)
    ltcg_taxable = max(D(0), delivery_ltcg - rules.ltcg_exemption)
    with_trading = _tax(rules, regime, other + spec_income, stcg, ltcg_taxable)
    without = _tax(rules, regime, other, D(0), D(0))
    incremental = with_trading["total"] - without["total"]
    return {
        "label": "ESTIMATED income tax - informational only, not a trading cost", "financial_year": fy, "regime": regime,
        "rules": {"effective_from": rules.effective_from, "source": rules.source, "verified": rules.verified, "note": rules.note},
        "intraday_speculative": {"net_after_charges": str(_r(intraday_net)), "brought_forward_loss_used": str(_r(min(max(D(0), intraday_net), brought_forward_speculative_loss))),
                                 "taxable_speculative_income": str(_r(spec_income)), "loss_to_carry_forward": str(_r(carry_forward)),
                                 "treatment": "speculative business income (s.43(5)); loss set off only against speculative income; not capital gains"},
        "delivery_capital_gains": {"stcg": str(_r(stcg)), "ltcg": str(_r(max(D(0), delivery_ltcg))), "ltcg_exemption": str(rules.ltcg_exemption), "ltcg_taxable": str(_r(ltcg_taxable)),
                                   "treatment": "STCG s.111A / LTCG s.112A; never applied to intraday"},
        "tax_with_trading": {k: str(_r(v)) if k != "surcharge_rate" else str(v) for k, v in with_trading.items()},
        "tax_without_trading": str(_r(without["total"])),
        "estimated_additional_tax": str(_r(max(D(0), incremental))),
        "excluded": ["F&O (separate category)", "advance-tax interest", "marginal relief on surcharge", "AMT", "other income heads"],
        "disclaimer": "Estimate for planning only; configurable by FY and regime; confirm with a qualified tax professional.",
    }
