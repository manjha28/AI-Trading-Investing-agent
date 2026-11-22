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
            cached = DhanAdapter._scrip_master_cache
            if cached is None:                       # never load the 230,000-row master inside a request: warm it in the background and answer from the previous copy (or none) meanwhile
                _warm_in_background()
                return _cache[1] if _cache else frozenset()
            rows = cached[1]
        else:
            rows = loader()
        syms = frozenset((r.get("UNDERLYING_SYMBOL") or "").strip().upper() for r in rows
                         if r.get("EXCH_ID") == "NSE" and r.get("SEGMENT") == "E" and (r.get("ISIN") or "").strip().upper().startswith("INF"))
        _cache = (time.monotonic(), syms)
        return syms
    except Exception as exc:  # noqa: BLE001 - classification must never break research; keep the previous copy
        logger.warning("fund_symbols_unavailable error_class=%s", type(exc).__name__)
        return _cache[1] if _cache else frozenset()


_warming = False


def _warm_in_background() -> None:
    global _warming
    if _warming:
        return
    _warming = True

    def run() -> None:
        global _warming
        try:
            from app import dhan_master_warm
            dhan_master_warm.warm_once()
        except Exception as exc:  # noqa: BLE001
            logger.warning("fund_master_warm_failed error_class=%s", type(exc).__name__)
        finally:
            _warming = False
    import threading
    threading.Thread(target=run, name="dhan-master-warm", daemon=True).start()


def is_fund(instrument: Any) -> bool:
    return (getattr(instrument, "symbol", "") or "").strip().upper() in fund_symbols()


def reset_cache() -> None:
    global _cache
    _cache = None
