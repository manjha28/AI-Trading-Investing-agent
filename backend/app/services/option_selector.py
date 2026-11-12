"""Option contract selection using ONLY information available at the signal timestamp.

Every rejection carries a machine-readable reason. A missing contract master, quote, depth, delta or lot size means NO TRADE: nothing is estimated,
substituted or filled from the current chain. Greeks come from the provider quote at that time; they are calculated only from a contemporaneous
implied volatility (labelled ``calculated``) and otherwise the contract is rejected.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from app.services.options_data import IST, Contract, OptionDataSource, Quote, aware

D = Decimal
YEAR_SECONDS = 365.0 * 24 * 3600


@dataclass(frozen=True)
class SelectionConfig:
    allow_ce: bool = True
    allow_pe: bool = True
    strike_choices: tuple[str, ...] = ("atm", "itm1")       # subset of atm, itm1
    delta_min: float = 0.45
    delta_max: float = 0.60
    delta_preferred: float = 0.55
    expiry_rank: int = 0                                    # 0 = nearest eligible expiry
    allow_expiry_day: bool = False
    max_spread_pct: float = 0.75                            # of the bid/ask midpoint
    quote_max_age_seconds: float = 15.0
    min_volume: int = 0
    min_depth_lots: float = 1.0                             # min(bid_qty, ask_qty) must cover this many lots; 0 disables
    allow_calculated_greeks: bool = True
    risk_free_rate_pct: float = 6.5                         # only used for calculated delta


@dataclass
class Selection:
    ok: bool
    direction: str
    option_type: str
    signal_time: datetime
    reference_price: Decimal | None
    reference_label: str
    contract: Contract | None = None
    quote: Quote | None = None
    delta: float | None = None
    delta_source: str | None = None
    spread_pct: float | None = None
    expiry_date: str | None = None
    why: str = ""
    no_trade_reasons: list[str] = field(default_factory=list)
    candidates: list[dict[str, Any]] = field(default_factory=list)
    master_version: str | None = None

    def as_dict(self) -> dict[str, Any]:
        c, q = self.contract, self.quote
        return {"ok": self.ok, "direction": self.direction, "option_type": self.option_type, "signal_time": self.signal_time.isoformat(), "reference_price": None if self.reference_price is None else str(self.reference_price),
                "reference_label": self.reference_label, "symbol": c.symbol if c else None, "contract_id": c.id if c else None, "strike": str(c.strike) if c else None, "expiry": self.expiry_date,
                "lot_size": c.lot_size if c else None, "bid": str(q.bid) if q and q.bid is not None else None, "ask": str(q.ask) if q and q.ask is not None else None,
                "quote_time": q.time.isoformat() if q else None, "delta": self.delta, "delta_source": self.delta_source, "spread_pct": self.spread_pct, "why": self.why,
                "no_trade_reasons": self.no_trade_reasons, "candidates": self.candidates, "master_version": self.master_version}


def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def calculated_delta(option_type: str, spot: float, strike: float, expiry: datetime, at: datetime, iv_pct: float | None, rate_pct: float) -> float | None:
    """Black-Scholes delta from a CONTEMPORANEOUS implied volatility. Returns None when any input is unusable; never defaults a missing IV."""
    t = (aware(expiry) - aware(at)).total_seconds() / YEAR_SECONDS
    if t <= 0 or spot <= 0 or strike <= 0 or iv_pct is None or iv_pct <= 0:
        return None
    sigma = iv_pct / 100.0
    d1 = (math.log(spot / strike) + (rate_pct / 100.0 + 0.5 * sigma * sigma) * t) / (sigma * math.sqrt(t))
    return _ncdf(d1) if option_type == "CE" else _ncdf(d1) - 1.0


def _delta_for(q: Quote, c: Contract, ref: Decimal, cfg: SelectionConfig) -> tuple[float | None, str | None]:
    if q.delta is not None and q.greeks_source in ("provider", "calculated"):
        return float(q.delta), q.greeks_source
    if cfg.allow_calculated_greeks and q.implied_volatility is not None:
        spot = float(q.underlying_price) if q.underlying_price is not None else float(ref)
        d = calculated_delta(c.option_type, spot, float(c.strike), c.expiry, q.time, float(q.implied_volatility), cfg.risk_free_rate_pct)
        if d is not None:
            return d, "calculated"
    return None, None


def select_contract(src: OptionDataSource, cfg: SelectionConfig, *, underlying: str, direction: str, signal_time: datetime, reference_price: Decimal | None,
                    reference_label: str = "spot") -> Selection:
    otype = "CE" if direction == "long" else "PE"
    ts = aware(signal_time)
    sel = Selection(False, direction, otype, ts, reference_price, reference_label)

    def fail(reason: str) -> Selection:
        sel.no_trade_reasons.append(reason)
        sel.why = "NO TRADE: " + reason
        return sel

    if (otype == "CE" and not cfg.allow_ce) or (otype == "PE" and not cfg.allow_pe):
        return fail(f"{otype}_disabled")
    if reference_price is None or reference_price <= 0:
        return fail("no_reference_price_for_atm")
    contracts = [c for c in src.contracts_as_of(underlying, ts) if c.option_type == otype]
    if not contracts:
        return fail("no_contract_master_for_timestamp")     # master missing entirely, or nothing valid/unexpired at the signal time
    today = ts.astimezone(IST).date()
    expiries = sorted({c.expiry_date for c in contracts if c.expiry_date >= today and (cfg.allow_expiry_day or c.expiry_date > today)})
    if len(expiries) <= cfg.expiry_rank:
        return fail("only_same_day_expiry_available" if any(c.expiry_date == today for c in contracts) else "no_eligible_expiry")
    expiry = expiries[cfg.expiry_rank]
    sel.expiry_date = expiry.isoformat()
    chain = sorted((c for c in contracts if c.expiry_date == expiry), key=lambda c: c.strike)
    sel.master_version = chain[0].master_version
    strikes = [c.strike for c in chain]
    atm_i = min(range(len(strikes)), key=lambda i: (abs(strikes[i] - reference_price), strikes[i]))   # ties: the lower strike (deterministic)
    picks: dict[str, int] = {"atm": atm_i, "itm1": atm_i - 1 if otype == "CE" else atm_i + 1}   # ITM call = lower strike, ITM put = higher strike; spacing comes from the master
    cands: list[tuple[float, float, Contract, Quote, float, str, float]] = []
    for name in cfg.strike_choices:
        i = picks.get(name)
        rec: dict[str, Any] = {"strike_choice": name, "rejected": []}
        sel.candidates.append(rec)
        if i is None or not (0 <= i < len(chain)):
            rec["rejected"].append("strike_not_listed")
            continue
        c = chain[i]
        rec.update(symbol=c.symbol, strike=str(c.strike), contract_id=c.id)
        rej: list[str] = rec["rejected"]
        if not c.symbol or c.strike <= 0 or c.lot_size is None or c.lot_size <= 0:
            rej.append("contract_identity_or_lot_size_unverified")
        q = src.latest_quote(c.id, ts)
        spread: float | None = None
        delta: float | None = None
        dsrc: str | None = None
        if q is None:
            rej.append("no_quote_at_timestamp")
        else:
            rec["quote_time"] = q.time.isoformat()
            age = (ts - aware(q.time)).total_seconds()
            if age > cfg.quote_max_age_seconds:
                rej.append(f"stale_quote_{int(age)}s")
            if q.bid is None or q.ask is None or q.bid <= 0 or q.ask <= 0 or q.ask < q.bid:
                rej.append("bid_or_ask_missing_or_crossed")
            else:
                mid = (q.bid + q.ask) / 2
                spread = float((q.ask - q.bid) / mid * 100)
                rec["spread_pct"] = round(spread, 4)
                if spread > cfg.max_spread_pct:
                    rej.append(f"spread_{spread:.2f}pct_exceeds_{cfg.max_spread_pct}")
            if cfg.min_depth_lots > 0 and c.lot_size:
                need = cfg.min_depth_lots * c.lot_size
                if q.bid_qty is None or q.ask_qty is None:
                    rej.append("depth_unavailable")
                elif min(q.bid_qty, q.ask_qty) < need:
                    rej.append(f"insufficient_depth_{min(q.bid_qty, q.ask_qty)}_below_{int(need)}")
            if cfg.min_volume > 0 and (q.volume is None or q.volume < cfg.min_volume):
                rej.append("illiquid_volume_missing_or_below_minimum")
            delta, dsrc = _delta_for(q, c, reference_price, cfg)
            if delta is None:
                rej.append("delta_unavailable")        # never invented
            else:
                rec.update(delta=round(delta, 4), delta_source=dsrc)
                if not (cfg.delta_min <= abs(delta) <= cfg.delta_max):
                    rej.append(f"delta_{abs(delta):.2f}_outside_{cfg.delta_min}-{cfg.delta_max}")
        if not rej and q is not None and delta is not None and spread is not None:
            cands.append((abs(abs(delta) - cfg.delta_preferred), float(abs(c.strike - reference_price)), c, q, delta, dsrc or "", spread))
    if not cands:
        sel.no_trade_reasons += sorted({r for rec in sel.candidates for r in rec["rejected"]}) or ["no_candidate_strikes"]
        sel.why = "NO TRADE: " + ", ".join(sel.no_trade_reasons)
        return sel
    cands.sort(key=lambda t: (t[0], t[1], t[2].strike))
    _, _, c, q, delta, dsrc, spread = cands[0]
    sel.ok, sel.contract, sel.quote, sel.delta, sel.delta_source, sel.spread_pct = True, c, q, delta, dsrc, round(spread, 4)
    sel.why = (f"{otype} {c.strike} expiring {expiry} (nearest eligible expiry; same-day {'allowed' if cfg.allow_expiry_day else 'excluded'}); |delta| {abs(delta):.3f} ({dsrc}) closest to preferred "
               f"{cfg.delta_preferred} among {len(cands)} eligible of {len(cfg.strike_choices)} strike choices; ATM referenced to {reference_label} {reference_price}; spread {spread:.2f}% of midpoint; "
               f"quote age {(ts - aware(q.time)).total_seconds():.0f}s")
    return sel
