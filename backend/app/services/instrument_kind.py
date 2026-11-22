"""Is this NSE instrument a fund (ETF, index fund, liquid fund, gilt fund) rather than a company's shares? Source: the Dhan instrument master's ISIN (Indian mutual-fund / ETF ISINs start 'INF',
company shares 'INE'; Dhan first, no other source). Used to keep funds out of stock research, the liquidity report and backtests; they stay in the app everywhere else (quotes, charts, holdings).
No data is changed. Unknown (master unavailable) is NOT treated as a fund."""
from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)
TTL_SECONDS = 12 * 3600
_cache: tuple[float, frozenset[str]] | None = None


def fund_symbols(*, loader: Any = None) -> frozenset[str]:
    """NSE underlying symbols whose Dhan-master ISIN is a fund ISIN (INF...). Cached for 12 h; an unreachable master returns the previous copy, or empty."""
    global _cache
    if _cache is not None and time.monotonic() - _cache[0] < TTL_SECONDS:
        return _cache[1]
    try:
        if loader is None:
            from app.broker_adapters import DhanAdapter
            rows = DhanAdapter._scrip_master(force=False)
        else:
            rows = loader()
        syms = frozenset((r.get("UNDERLYING_SYMBOL") or "").strip().upper() for r in rows
                         if r.get("EXCH_ID") == "NSE" and r.get("SEGMENT") == "E" and (r.get("ISIN") or "").strip().upper().startswith("INF"))
        _cache = (time.monotonic(), syms)
        return syms
    except Exception as exc:  # noqa: BLE001 - classification must never break research; keep the previous copy
        logger.warning("fund_symbols_unavailable error_class=%s", type(exc).__name__)
        return _cache[1] if _cache else frozenset()


def is_fund(instrument: Any) -> bool:
    return (getattr(instrument, "symbol", "") or "").strip().upper() in fund_symbols()


def reset_cache() -> None:
    global _cache
    _cache = None
