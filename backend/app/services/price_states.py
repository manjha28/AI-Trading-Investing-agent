"""Canonical price-state vocabulary. Import these names; do not define price-state strings elsewhere.

Two related but different vocabularies:

- Freshness states classify one quote or bar observation (``price_freshness.classify_quote``).
- Valuation basis says which price a portfolio figure was computed from.

``basis_for_freshness`` maps a freshness state to the basis that labels it: live -> live,
delayed -> delayed, session_close -> session_close, prior_close -> prior_close. Every other freshness state (stale,
unknown, unverified, synthetic, ...) yields no basis, so the figure is shown as unavailable.
``prior_close`` is a valuation basis when today has already traded and the price is from the
most recent completed session; that relabelling is made by
``price_freshness.valuation_basis_for_quote``, not here.

The map is a label, not a gate. ``delayed`` is a valuation basis, never an action basis: a
delayed price may be valued only where a factual figure at a stated time is shown, under the
conditions in ``price_freshness.delayed_price_acceptable_for_valuation``. It is not in
``USABLE_FRESHNESS_STATUSES``, so alerts, risk metrics, signals and thesis monitoring keep
suppressing it.

The frontend mirrors these values in ``frontend/src/lib/priceStates.ts``; keep them in sync.
"""

from typing import Final

FRESHNESS_LIVE: Final = "live"
FRESHNESS_DELAYED: Final = "delayed"
FRESHNESS_SESSION_CLOSE: Final = "session_close"
FRESHNESS_PRIOR_CLOSE: Final = "prior_close"
FRESHNESS_STALE: Final = "stale"
FRESHNESS_UNKNOWN: Final = "unknown"
FRESHNESS_UNAVAILABLE: Final = "unavailable"
FRESHNESS_SYNTHETIC: Final = "synthetic"
FRESHNESS_UNVERIFIED: Final = "unverified"
FRESHNESS_HISTORICAL: Final = "historical"
FRESHNESS_FUTURE_DATED: Final = "future_dated"

BASIS_LIVE: Final = "live"
BASIS_DELAYED: Final = "delayed"
BASIS_SESSION_CLOSE: Final = "session_close"
BASIS_PRIOR_CLOSE: Final = "prior_close"
BASIS_BROKER_SNAPSHOT: Final = "broker_snapshot"
BASIS_UNAVAILABLE: Final = "unavailable"

VALUATION_BASES: Final = (
    BASIS_LIVE,
    BASIS_DELAYED,
    BASIS_SESSION_CLOSE,
    BASIS_PRIOR_CLOSE,
    BASIS_BROKER_SNAPSHOT,
    BASIS_UNAVAILABLE,
)


def basis_for_freshness(status: str) -> str | None:
    """Return the valuation basis that labels a freshness state, else None.

    An open-market tick that arrived too late is ``delayed``: it is labelled as such and is
    never relabelled ``live``.
    """
    if status == FRESHNESS_LIVE:
        return BASIS_LIVE
    if status == FRESHNESS_DELAYED:
        return BASIS_DELAYED
    if status == FRESHNESS_SESSION_CLOSE:
        return BASIS_SESSION_CLOSE
    if status == FRESHNESS_PRIOR_CLOSE:
        return BASIS_PRIOR_CLOSE
    return None


def provider_family(source: str) -> str:
    """Map a quote source to its provider family (``dhan_ws`` -> ``dhan``).

    Two prices are same-source only when their provider families are equal.
    """
    return source.removesuffix("_ws")
