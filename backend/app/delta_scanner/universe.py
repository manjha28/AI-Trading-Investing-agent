"""Universe for the scanner: a pasted list of symbols, or the most liquid USD perpetuals by 24 h turnover with a tight spread. Public Delta tickers only."""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

D = Decimal
SYMBOL = re.compile(r"^[A-Z0-9]{2,24}USD$")


def parse_symbols(text: str) -> list[str]:
    """Comma, space or newline separated; upper-cased, de-duplicated, USD perpetual names only. Raises ValueError listing what was refused."""
    raw = [s.strip().upper() for s in re.split(r"[,\s;]+", text or "") if s.strip()]
    bad = [s for s in raw if not SYMBOL.match(s)]
    if bad:
        raise ValueError("Not USD-quoted perpetual symbols: " + ", ".join(bad[:10]))
    return list(dict.fromkeys(raw))


def spread_bps(t: dict[str, Any]) -> Decimal | None:
    q = t.get("quotes") or {}
    try:
        bid, ask = D(str(q["best_bid"])), D(str(q["best_ask"]))
    except (KeyError, InvalidOperation, TypeError):
        return None
    mid = (bid + ask) / 2
    return None if mid <= 0 or ask < bid else (ask - bid) / mid * D(10000)


def top_liquid(tickers: list[dict[str, Any]], n: int = 10, max_spread_bps: Decimal = D(10)) -> list[dict[str, Any]]:
    """Perpetual USD contracts ranked by 24 h turnover (USD), keeping only those with a known spread under the limit. Each row: symbol, turnover, spread, funding."""
    rows = []
    for t in tickers:
        sym = str(t.get("symbol", ""))
        if not SYMBOL.match(sym) or t.get("contract_type") not in (None, "perpetual_futures"):
            continue
        try:
            turnover = D(str(t.get("turnover_usd")))
        except InvalidOperation:
            continue
        sp = spread_bps(t)
        if sp is None or sp > max_spread_bps or turnover <= 0:
            continue
        rows.append({"symbol": sym, "turnover_usd": str(turnover.quantize(D("1"))), "spread_bps": str(sp.quantize(D("0.01"))), "funding_rate": None if t.get("funding_rate") is None else str(t["funding_rate"])})
    rows.sort(key=lambda r: -D(r["turnover_usd"]))
    return rows[:n]


def fetch_tickers() -> list[dict[str, Any]]:
    import httpx
    r = httpx.get("https://api.india.delta.exchange/v2/tickers", params={"contract_types": "perpetual_futures"}, timeout=20)
    r.raise_for_status()
    return r.json().get("result") or []
