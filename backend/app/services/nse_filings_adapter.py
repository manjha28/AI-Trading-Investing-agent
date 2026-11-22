"""NSE public-filings adapter for governance data (auditor resignations, regulatory orders, pledge changes, ASM/GSM/T2T lists, results dates). DISABLED (``nse_filings_enabled`` is False) and it
makes NO network call of any kind in this build: NSE's terms of use have not been read or reported to the owner, which Phase 6 requires before enabling. With the flag off every function raises
``AdapterDisabled`` and the callers treat the fields as UNKNOWN (never as clear). When enabled later it must use a polite rate limit, a cache and store raw files with their download date.
This module deliberately contains no HTTP client."""
from app.config import settings


class AdapterDisabled(RuntimeError):
    pass


def enabled() -> bool:
    return bool(getattr(settings, "nse_filings_enabled", False))


def fetch_governance_events(symbol: str) -> list[dict]:
    if not enabled():
        raise AdapterDisabled("The NSE filings adapter is disabled (nse_filings_enabled=false); governance fields stay unknown.")
    raise NotImplementedError("Enabling requires reading NSE's terms of use and an owner-approved fetcher; no fetcher exists in this build.")
