"""Is this NSE instrument a fund (ETF, index fund, liquid fund, gilt fund) rather than a company's shares? Source: the Dhan instrument master's ISIN (Indian mutual-fund / ETF ISINs start 'INF',
company shares 'INE'; Dhan first, no other source). Used to keep funds out of stock research, the liquidity report and backtests; they stay in the app everywhere else (quotes, charts, holdings).
No data is changed. Unknown (master unavailable) is NOT treated as a fund."""
from __future__ import annotations

import logging
import re
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


_FUND_NAME = re.compile(r"\b(ETF|MUTUAL FUND|INDEX FUND|LIQUID FUND)\b|BEES|SETF|INAV|LIQUID|GILT", re.IGNORECASE)


def is_fund(instrument: Any) -> bool:
    """The kind stored on the instrument row (set when the master is synced) decides; only an unclassified row falls back to the in-memory master, so a fresh process never depends on it for a classified stock."""
    if (getattr(instrument, "asset_class", None) or "") == "etf":
        return True
    kind = getattr(instrument, "instrument_kind", None)
    if kind in ("fund", "stock"):
        return kind == "fund"
    return (getattr(instrument, "symbol", "") or "").strip().upper() in fund_symbols()


def sync_kinds(db: Any, *, rows: Any = None) -> dict[str, int]:
    """Store 'fund' or 'stock' on every Indian NSE/BSE equity instrument found in the Dhan master (ISIN prefix INF = fund). Symbols not in the master keep their value (NULL stays NULL = unknown, never 'stock').
    Returns counts. ``rows`` is the master (tests pass fixtures; production reads the loaded master)."""
    from sqlalchemy import select

    from app.models import Instrument
    if rows is None:
        from app.broker_adapters import DhanAdapter
        cached = DhanAdapter._scrip_master_cache
        if cached is None:
            return {"updated": 0, "skipped_no_master": 1}
        rows = cached[1]
    kinds: dict[str, str] = {}
    by_secid: dict[str, str] = {}                       # Dhan security id -> kind: the master's symbol can differ from ours (SBIMID150 vs SBINMID150) but the id is the same instrument
    for r in rows:
        if r.get("EXCH_ID") != "NSE" or r.get("SEGMENT") != "E":
            continue
        sym = (r.get("UNDERLYING_SYMBOL") or "").strip().upper()
        isin = (r.get("ISIN") or "").strip().upper()
        if not isin:
            continue
        kind = "fund" if isin.startswith("INF") else "stock" if isin.startswith("INE") else None   # INF = mutual fund / ETF units, INE = equity shares
        if kind is None:
            continue
        if r.get("SECURITY_ID"):
            by_secid[str(r["SECURITY_ID"]).strip()] = kind
        if sym and (kind == "fund" or sym not in kinds):
            kinds[sym] = kind
    secid_of: dict[Any, str] = {}
    from app.models import InstrumentProviderIdentifier
    for iid, pid in db.execute(select(InstrumentProviderIdentifier.instrument_id, InstrumentProviderIdentifier.provider_instrument_id).where(InstrumentProviderIdentifier.provider == "dhan")):
        secid_of[iid] = str(pid).split("|")[0].strip()
    updated = 0
    for inst in db.scalars(select(Instrument).where(Instrument.country == "IN", Instrument.asset_class.in_(("equity", "etf")), Instrument.is_synthetic.is_(False))):
        k = "fund" if inst.asset_class == "etf" else kinds.get((inst.symbol or "").strip().upper())
        if k is None:
            k = by_secid.get(secid_of.get(inst.id, ""))     # ISIN prefix through the Dhan security id when the symbol is not in the master
        if k is None and _FUND_NAME.search(inst.display_name or ""):
            k = "fund"                                   # not in the master by this symbol, but the stored name says ETF / mutual fund (never the other way: an unknown name is not a stock)
        if k is not None and inst.instrument_kind != k:
            inst.instrument_kind = k
            updated += 1
    db.commit()
    return {"updated": updated, "funds": sum(1 for v in kinds.values() if v == "fund"), "stocks": sum(1 for v in kinds.values() if v == "stock")}


def reset_cache() -> None:
    global _cache
    _cache = None
