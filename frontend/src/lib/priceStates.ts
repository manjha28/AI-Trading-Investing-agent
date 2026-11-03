// Manual mirror of backend/app/services/price_states.py. Keep the values identical.
// Cross-language sharing is not possible, so a change on either side must be made on both.

export const BASIS_LIVE = 'live';
export const BASIS_DELAYED = 'delayed';
export const BASIS_SESSION_CLOSE = 'session_close';
export const BASIS_PRIOR_CLOSE = 'prior_close';
export const BASIS_BROKER_SNAPSHOT = 'broker_snapshot';
export const BASIS_UNAVAILABLE = 'unavailable';
export const FRESHNESS_PRIOR_CLOSE = 'prior_close';

export type ValuationBasis =
  | typeof BASIS_LIVE
  | typeof BASIS_DELAYED
  | typeof BASIS_SESSION_CLOSE
  | typeof BASIS_PRIOR_CLOSE
  | typeof BASIS_BROKER_SNAPSHOT
  | typeof BASIS_UNAVAILABLE;

export type MarketDataStatus = 'live' | 'delayed' | 'session_close' | 'unavailable' | 'synthetic' | 'unknown';

// Freshness and basis are related but not identical. `live`, `delayed`, `session_close` and
// `prior_close` freshness each label a valuation basis; `historical` and the rest have none.
// Delayed and prior-close prices label factual figures only, never action bases.
export function basisForFreshnessStatus(status: string | null | undefined): ValuationBasis | null {
  if (status === 'live') return BASIS_LIVE;
  if (status === 'delayed') return BASIS_DELAYED;
  if (status === 'session_close') return BASIS_SESSION_CLOSE;
  if (status === 'prior_close') return BASIS_PRIOR_CLOSE;
  return null;
}

export type PriceStateRow = {
  freshness: string;
  basis?: string | null;
  is_synthetic?: boolean;
};

// One derivation for the header and the sidebar. A row without a usable basis makes the
// status unavailable, so a session-close or delayed price can never read as "live". A
// `delayed` basis is the labelled form of "older than the freshness window", so a row that
// carries it is not treated as the unusable `stale` state.
export function deriveMarketDataStatus(rows: PriceStateRow[] | null | undefined): MarketDataStatus {
  if (rows === null || rows === undefined) return 'unknown';
  if (rows.length === 0) return 'unavailable';
  if (rows.some((row) => row.freshness === 'unavailable' || (row.freshness === 'stale' && row.basis !== BASIS_DELAYED && row.basis !== BASIS_PRIOR_CLOSE))) {
    return 'unavailable';
  }
  if (rows.some((row) => (row.basis ?? BASIS_UNAVAILABLE) === BASIS_UNAVAILABLE)) return 'unavailable';
  if (rows.some((row) => row.is_synthetic)) return 'synthetic';
  if (rows.some((row) => row.basis === BASIS_DELAYED)) return 'delayed';
  if (rows.some((row) => row.basis === BASIS_SESSION_CLOSE || row.basis === BASIS_PRIOR_CLOSE)) {
    return 'session_close';
  }
  return 'live';
}

// "14 min old". Rounds down, and never reads as fresher than it is. Null when the backend
// supplied no age (a stored daily bar has no provider tick time).
export function formatPriceAge(seconds: number | null | undefined): string | null {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds) || seconds < 0) return null;
  if (seconds < 60) return 'under 1 min old';
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} min old`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return `${hours} h ${minutes % 60} min old`;
  return `${Math.floor(hours / 24)} d old`;
}

// What the price actually is, for display next to a number. "Live" only for a live basis.
export function basisCaption(basis: string | null | undefined): string {
  switch (basis) {
    case BASIS_LIVE:
      return 'Live';
    case BASIS_DELAYED:
      return 'Delayed';
    case BASIS_SESSION_CLOSE:
      return 'Closing price';
    case BASIS_PRIOR_CLOSE:
      return 'Previous close';
    case BASIS_BROKER_SNAPSHOT:
      return 'Broker snapshot';
    default:
      return 'Unavailable';
  }
}

export function marketStatusLabel(status: MarketDataStatus): string {
  switch (status) {
    case 'live':
      return 'Live';
    case 'delayed':
      return 'Delayed';
    case 'session_close':
      return 'Closing prices';
    case 'synthetic':
      return 'Synthetic';
    case 'unknown':
      return 'Checking…';
    default:
      return 'Unavailable';
  }
}

export function basisLabel(basis: string | null | undefined): string {
  switch (basis) {
    case BASIS_LIVE:
      return 'Live';
    case BASIS_DELAYED:
      return 'Delayed';
    case BASIS_SESSION_CLOSE:
      return 'Session close';
    case BASIS_PRIOR_CLOSE:
      return 'Prior close';
    case BASIS_BROKER_SNAPSHOT:
      return 'Broker snapshot';
    default:
      return 'Unavailable';
  }
}
