import { useCallback, useEffect, useRef, useState } from 'react';
import { Info } from 'lucide-react';
import { apiRequest, isAbortError } from '../api';
import { formatMoney, formatQuantity, type WorkspaceState } from '../components/useWorkspace';
import { TransactionEntry } from '../components/TransactionEntry';
import { ThesisEditor } from '../components/ThesisEditor';
import { sessionCloseForVenue } from '../components/CandleChart';
import { ErrorState } from '../components/ui';
import { useWatchlistStream } from '../hooks/useWatchlistStream';
import {
  BASIS_DELAYED,
  BASIS_SESSION_CLOSE,
  BASIS_UNAVAILABLE,
  basisCaption,
  formatPriceAge,
  resolveMarketPrice,
  type PriceStateRow,
  type ResolvedMarketPrice,
  type ValuationBasis,
} from '../lib/priceStates';

type Props = Pick<
  WorkspaceState,
  | 'brokerRefreshKey'
  | 'cancelEditingTransaction'
  | 'coverage'
  | 'addInstrumentToWatchlist'
  | 'csvContent'
  | 'csvPreview'
  | 'currency'
  | 'editingTransaction'
  | 'exportTransactionCsv'
  | 'importTransactionCsv'
  | 'importingCsv'
  | 'loadTransactionCsv'
  | 'loadingPortfolio'
  | 'marketDataStatus'
  | 'portfolioId'
  | 'previewTransactionCsv'
  | 'refreshPortfolioValuation'
  | 'refreshingValuation'
  | 'removeInstrumentFromWatchlist'
  | 'riskMetrics'
  | 'setActiveView'
  | 'setBrokerPriceRows'
  | 'setOpenMarketInstrumentId'
  | 'setCoverageVersion'
  | 'setThesisSymbol'
  | 'setTransactionType'
  | 'submitTransaction'
  | 'summary'
  | 'thesisSymbol'
  | 'transactionType'
  | 'valuation'
  | 'valuationBySymbol'
  | 'watchlist'
  | 'watchlistLoadState'
  | 'watchlistLoadError'
  | 'refreshWatchlist'
>;

// Per-row valuation exactly as the backend returns it. Nothing here is recomputed.
type RowValuation = {
  status: ValuationBasis;
  basis: ValuationBasis;
  price: string | null;
  provider_timestamp: string | null;
  // Seconds between the provider's tick time and the server's response, measured server-side.
  price_age_seconds: number | null;
  source: string | null;
  market_value: string | null;
  unrealized_profit: string | null;
  day_change: string | null;
  prior_close: string | null;
  quote_at: string | null;
  price_source: 'ltp' | 'close' | 'unavailable';
  as_of: string | null;
  session_state: 'open' | 'closed' | 'no_continuous_trading' | 'unknown';
  close_pending: boolean;
  stale_reason: string | null;
  day_change_reason: string | null;
};

type MappingCandidate = {
  symbol: string;
  security_id: string | null;
  isin: string | null;
  provider_instrument_id: string | null;
  segment: string | null;
  listing_date: string | null;
  status: string;
  reason: string | null;
  verification_required: string | null;
};

type BrokerRow = {
  symbol: string;
  exchange: string;
  exchange_raw: string;
  exchange_resolution_reason: string | null;
  product: string;
  security_id: string | null;
  instrument_id: string | null;
  quantity: string;
  average_price: string | null;
  synced_at: string;
};

type DhanHolding = BrokerRow & { isin: string | null; live_valuation: RowValuation };

type DhanPosition = BrokerRow & {
  position_type: string | null;
  realized_profit: string | null;
  unrealized_profit: string | null;
  broker_snapshot_at: string;
  live_valuation: RowValuation;
};

type ClosedRow = {
  symbol: string;
  exchange: string;
  exchange_raw: string;
  exchange_resolution_reason: string | null;
  product: string;
  security_id: string | null;
  quantity: string;
  average_price: string | null;
  position_type: string | null;
  realized_profit: string | null;
  unrealized_profit: string | null;
  synced_at: string;
};

type DhanPortfolio = {
  source: 'dhan';
  currency: 'INR';
  synced_at: string | null;
  snapshot_min_interval_seconds: number;
  market_session: { venue: string; state: 'open' | 'closed' | 'no_continuous_trading' | 'unknown'; reason: string } | null;
  instrument_master: { last_refreshed_at: string | null; mapping_candidates: MappingCandidate[] };
  holdings: DhanHolding[];
  positions: DhanPosition[];
  closed_positions: ClosedRow[];
  delivery_sales: ClosedRow[];
};

type PortfolioFigure =
  | 'cash'
  | 'invested_cost'
  | 'positions_exposure'
  | 'holdings_unrealized_pnl'
  | 'positions_unrealized_pnl'
  | 'market_value'
  | 'total_value'
  | 'unrealized_pnl'
  | 'day_change';

type EvidenceObservation = {
  symbol: string;
  source: string;
  timestamp: string | null;
  price_basis?: ValuationBasis;
  price_age_seconds?: number | null;
  basis_date?: string;
  provider_timestamp?: string | null;
  provider_attempts?: string[];
  quantity?: string;
  ltp?: string;
  close?: string;
  average_cost?: string | null;
  amount?: string;
};

type FigureProvenance = {
  type: 'broker_supplied' | 'derived' | 'combined';
  description: string;
  quote_observations?: EvidenceObservation[];
  previous_close_observations?: EvidenceObservation[];
  mirror_observations?: EvidenceObservation[];
  exclusions?: string[];
};

type BrokerPortfolioSummary = {
  currency: 'INR';
  sources: string[];
  totals: Record<PortfolioFigure, string | null> & { synced_at: string | null };
  brokers: {
    source: string;
    currency: 'INR';
    market_value: string | null;
    synced_at: string | null;
    unavailable_reasons: Partial<Record<PortfolioFigure, string>>;
    provenance: Partial<Record<PortfolioFigure, FigureProvenance>>;
  }[];
  value_comparison?: {
    alert: boolean;
    threshold: string;
    app_value: string | null;
    app_timestamp: string | null;
    broker_value: string | null;
    broker_timestamp: string | null;
    difference: string | null;
  };
};

type AlertEvent = {
  id: string;
  fired_at: string;
  severity: string;
  rule_type: string;
  status: string;
};

type ValuationHistoryPoint = {
  timestamp: string;
  currency: string;
  market_value: string | null;
  unrealized_pnl: string | null;
  day_change: string | null;
  invested: string | null;
  valuation_basis: string;
  source: string;
  provider_timestamp: string | null;
  complete: boolean;
  reason: string | null;
  price_observations: { symbol: string; source: string; timestamp: string; basis_date?: string; price_basis: string }[];
};

type ValuationHistoryResponse = {
  owner_wide: ValuationHistoryPoint[];
  portfolio_scoped: ValuationHistoryPoint[];
};

const HISTORY_RANGES = [
  { label: '1D', milliseconds: 24 * 60 * 60 * 1000 },
  { label: '1W', milliseconds: 7 * 24 * 60 * 60 * 1000 },
  { label: '1M', milliseconds: 30 * 24 * 60 * 60 * 1000 },
  { label: '3M', milliseconds: 90 * 24 * 60 * 60 * 1000 },
  { label: '6M', milliseconds: 180 * 24 * 60 * 60 * 1000 },
  { label: '1Y', milliseconds: 365 * 24 * 60 * 60 * 1000 },
] as const;

type WatchlistItem = Props['watchlist'][number];

const FIGURE_LABELS: Record<PortfolioFigure, string> = {
  cash: 'Cash balance',
  invested_cost: 'Invested cost (delivery holdings)',
  positions_exposure: 'Positions exposure (cost basis)',
  holdings_unrealized_pnl: 'Holdings unrealized P&L',
  positions_unrealized_pnl: 'Positions unrealized P&L',
  market_value: 'Portfolio value (holdings + positions)',
  total_value: 'Total value (cash + holdings + positions)',
  unrealized_pnl: 'Unrealized P&L (holdings + positions)',
  day_change: 'Day change (holdings + positions)',
};

const EVIDENCE_FIELDS = Object.keys(FIGURE_LABELS) as PortfolioFigure[];

const IST_TIME = new Intl.DateTimeFormat('en-GB', {
  timeZone: 'Asia/Kolkata',
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hour12: false,
});
const IST_DAY = new Intl.DateTimeFormat('en-GB', { timeZone: 'Asia/Kolkata', day: 'numeric', month: 'short' });

function money(value: string | number): string {
  return formatMoney(String(value), 'INR');
}

function signedMoney(value: string | number): string {
  return Number(value) > 0 ? `+${money(value)}` : money(value);
}

function tone(value: string | number | null): string | undefined {
  if (value === null) return undefined;
  return Number(value) >= 0 ? 'positive' : 'negative';
}

// "as of" in IST; the date is shown only when it is not today, so a stale price is never
// mistaken for a current one.
function istStamp(iso: string): string {
  const moment = new Date(iso);
  const sameDay = IST_DAY.format(moment) === IST_DAY.format(new Date());
  return `${sameDay ? '' : `${IST_DAY.format(moment)} `}${IST_TIME.format(moment)} IST`;
}

function sessionCloseLabel(sessionDate: string | null | undefined, venue: string | null | undefined): string {
  if (!sessionDate) return 'session date unavailable';
  const close = sessionCloseForVenue(venue);
  if (!close) return `${sessionDate} session close`;
  const day = IST_DAY.format(new Date(`${sessionDate.slice(0, 10)}T00:00:00Z`));
  return `${day} ${String(close.hour).padStart(2, '0')}:${String(close.minute).padStart(2, '0')} ${close.zoneLabel}`;
}

function Skeleton({ width = '6rem' }: { width?: string }) {
  return <span className="skeleton" role="status" aria-busy="true" aria-label="Loading" style={{ width }} />;
}

function UnavailableValue({ reason }: { reason: string }) {
  return (
    <span className="portfolio-unavailable" title={reason} aria-label={`Not available: ${reason}`}>
      <Info size={13} aria-hidden="true" />
      <span>Not available</span>
    </span>
  );
}

function relativeAge(seconds: number): string {
  if (seconds < 60) return 'Just now';
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 48 * 3600) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

function observationsOf(provenance: FigureProvenance | undefined): EvidenceObservation[] {
  return provenance
    ? [
        ...(provenance.quote_observations ?? []),
        ...(provenance.previous_close_observations ?? []),
        ...(provenance.mirror_observations ?? []),
      ]
    : [];
}

// Higher is weaker. A total takes the weakest basis among its priced observations, so "Live"
// is printed only when every one of them is live.
const BASIS_WEAKNESS: Record<string, number> = {
  live: 0,
  delayed: 1,
  session_close: 2,
  prior_close: 3,
  broker_snapshot: 4,
  unavailable: 5,
};

// Age in seconds at the moment of display. OverviewPage supplies it; see `ageNow` there.
type AgeNow = (base: number | null | undefined) => number | null;

// "14 min old", for a delayed price only. A live or closing price has no age to state.
function delayedAgeText(
  basis: string | null | undefined,
  base: number | null | undefined,
  ageNow: AgeNow,
): string | null {
  return basis === BASIS_DELAYED ? formatPriceAge(ageNow(base)) : null;
}

function resolveRowPrice(value: RowValuation): ResolvedMarketPrice {
  return resolveMarketPrice({
    price: value.price,
    price_source: value.price_source,
    provider: value.source,
    as_of: value.as_of,
    session_state: value.session_state,
    freshness: value.basis,
    close_pending: value.close_pending,
  });
}

function resolvedPriceText(
  price: ResolvedMarketPrice,
  age: string | null,
  venue: string | null | undefined,
): string {
  const provider = price.provider ?? 'market data';
  const session = `${venue ?? ''} ${price.session_state}`.trim();
  if (price.source === 'close') {
    return `Close · ${sessionCloseLabel(price.as_of, venue)} · ${provider} · ${session}`;
  }
  if (price.source === 'ltp') {
    return `LTP · ${price.as_of ? istStamp(price.as_of) : 'time unavailable'} · ${basisCaption(price.freshness)}${age ? ` · ${age}` : ''} · ${provider} · ${session}${price.close_pending ? ' · close pending' : ''}`;
  }
  return 'No usable market price';
}

function epochOf(timestamp: string | null): number {
  return timestamp ? new Date(timestamp).getTime() : Number.POSITIVE_INFINITY;
}

// Basis sub-line for a metric card, taken from the backend's observation basis.
function cardBasis(
  broker: BrokerPortfolioSummary['brokers'][number] | undefined,
  field: PortfolioFigure,
  ageNow: AgeNow,
  venue: string | null | undefined,
  sharedPriceLabel?: string,
): string {
  const provenance = broker?.provenance[field];
  if (!provenance) return '';
  if (sharedPriceLabel && provenance.type !== 'broker_supplied') return sharedPriceLabel;
  const observations = observationsOf(provenance);
  const priced = observations.filter((observation) => observation.price_basis);
  if (priced.length > 0) {
    const weakness = (observation: EvidenceObservation) => BASIS_WEAKNESS[observation.price_basis as string] ?? 0;
    const weakest = Math.max(...priced.map(weakness));
    // Among equally weak observations the oldest one labels the total.
    const labelling = priced
      .filter((observation) => weakness(observation) === weakest)
      .reduce((oldest, observation) => (epochOf(observation.timestamp) < epochOf(oldest.timestamp) ? observation : oldest));
    const age = delayedAgeText(labelling.price_basis, labelling.price_age_seconds, ageNow);
    const stamp = labelling.price_basis === BASIS_SESSION_CLOSE
      ? ` · ${sessionCloseLabel(labelling.basis_date ?? labelling.timestamp, venue)} · fetched ${labelling.provider_timestamp ? istStamp(labelling.provider_timestamp) : 'time unavailable'}`
      : labelling.timestamp ? ` · ${istStamp(labelling.timestamp)}` : '';
    const caption = labelling.price_basis === BASIS_SESSION_CLOSE ? 'Closing price' : basisCaption(labelling.price_basis);
    return `${caption}${age ? ` · ${age}` : ''}${stamp}`;
  }
  const timestamp = observations.find((observation) => observation.timestamp)?.timestamp;
  if (provenance.type === 'broker_supplied') return `Broker${timestamp ? ` · ${istStamp(timestamp)}` : ''}`;
  return '';
}

function MetricCard({
  label,
  value,
  loading,
  reason,
  basis,
  subline,
  signed = false,
}: {
  label: string;
  value: string | null;
  loading: boolean;
  reason: string;
  basis?: string;
  subline?: string;
  signed?: boolean;
}) {
  return (
    <article className="metric">
      <span className="portfolio-metric-label">{label}</span>
      <strong className={signed ? tone(value) : undefined}>
        {value === null ? (loading ? <Skeleton /> : <UnavailableValue reason={reason} />) : signed ? signedMoney(value) : money(value)}
      </strong>
      {value !== null && subline && <small className="portfolio-metric-basis">{subline}</small>}
      {value !== null && basis && <small className="portfolio-metric-basis">{basis}</small>}
    </article>
  );
}

function EvidenceList({ brokers, ageNow, venue }: { brokers: BrokerPortfolioSummary['brokers']; ageNow: AgeNow; venue: string | null | undefined }) {
  return (
    <details className="portfolio-evidence portfolio-evidence-card">
      <summary>Evidence behind each figure</summary>
      <p className="portfolio-evidence-note">
        Cash and open-position P&amp;L are broker-supplied. Market values use the freshest usable Dhan quote;
        when the session is shut that is the last completed-session close. INR only; no FX conversion.
      </p>
      {brokers.flatMap((broker) => EVIDENCE_FIELDS.map((field) => {
        const provenance = broker.provenance[field];
        const observations = observationsOf(provenance);
        const reasons = [...new Set([...(provenance?.exclusions ?? []), broker.unavailable_reasons[field] ?? ''])].filter(Boolean);
        return (
          <section className="portfolio-evidence" key={`${broker.source}-${field}`}>
            <h4>{FIGURE_LABELS[field]}</h4>
            {provenance && <p>{provenance.description}</p>}
            {observations.length > 0 && (
              <ul>
                {observations.map((observation, index) => {
                  const age = delayedAgeText(observation.price_basis, observation.price_age_seconds, ageNow);
                  return (
                    <li key={`${observation.symbol}-${observation.source}-${index}`}>
                      {observation.symbol} · {observation.source}
                      {observation.price_basis ? ` · ${basisCaption(observation.price_basis)}` : ''}
                      {age ? ` · ${age}` : ''}
                      {observation.price_basis === BASIS_SESSION_CLOSE || (!observation.price_basis && observation.basis_date)
                        ? ` · ${sessionCloseLabel(observation.basis_date ?? observation.timestamp, venue)} · fetched ${observation.provider_timestamp ? istStamp(observation.provider_timestamp) : 'time unavailable'}`
                        : observation.timestamp ? ` · ${istStamp(observation.timestamp)}` : ''}
                      {observation.quantity !== undefined ? ` · qty ${formatQuantity(observation.quantity)}` : ''}
                      {observation.ltp !== undefined ? ` · price ${money(observation.ltp)}` : ''}
                      {observation.close !== undefined ? ` · prior close ${money(observation.close)}` : ''}
                      {observation.average_cost ? ` · avg cost ${money(observation.average_cost)}` : ''}
                      {observation.amount ? ` · balance ${money(observation.amount)}` : ''}
                    </li>
                  );
                })}
              </ul>
            )}
            {reasons.length > 0 && <ul>{reasons.map((reason) => <li key={reason}>{reason}</li>)}</ul>}
          </section>
        );
      }))}
    </details>
  );
}

type SparkState = { status: 'loading' } | { status: 'empty' } | { status: 'ready'; segments: { x: number; y: number }[][] };

const SPARK_INTERVAL_MS = 5 * 60 * 1000;
const SPARK_WIDTH = 96;
const SPARK_HEIGHT = 26;

// Real stored 5-minute bars of the latest session. A hole longer than two bar intervals
// breaks the line; nothing is interpolated across it.
function buildSegments(bars: { timestamp: string; close: string }[]): { x: number; y: number }[][] {
  const points = bars
    .map((bar) => ({ t: new Date(bar.timestamp).getTime(), v: Number(bar.close) }))
    .filter((point) => Number.isFinite(point.t) && Number.isFinite(point.v));
  if (points.length === 0) return [];
  const lastDay = IST_DAY.format(points[points.length - 1].t);
  const session = points.filter((point) => IST_DAY.format(point.t) === lastDay);
  const t0 = session[0].t;
  const t1 = session[session.length - 1].t;
  const low = Math.min(...session.map((point) => point.v));
  const high = Math.max(...session.map((point) => point.v));
  const segments: { x: number; y: number }[][] = [];
  let previous: number | null = null;
  for (const point of session) {
    const x = t1 === t0 ? SPARK_WIDTH / 2 : ((point.t - t0) / (t1 - t0)) * SPARK_WIDTH;
    const y = high === low ? SPARK_HEIGHT / 2 : SPARK_HEIGHT - ((point.v - low) / (high - low)) * SPARK_HEIGHT;
    if (previous === null || point.t - previous > SPARK_INTERVAL_MS * 2) segments.push([]);
    segments[segments.length - 1].push({ x, y });
    previous = point.t;
  }
  return segments;
}

function Sparkline({ instrumentId, symbol }: { instrumentId: string; symbol: string }) {
  const [state, setState] = useState<SparkState>({ status: 'loading' });
  useEffect(() => {
    let cancelled = false;
    const end = new Date();
    end.setMinutes(Math.floor(end.getMinutes() / 5) * 5, 0, 0);
    const params = new URLSearchParams({
      interval: '5m',
      start: new Date(end.getTime() - 5 * 86_400_000).toISOString(),
      end: new Date(end.getTime() + 3_600_000).toISOString(),
      limit: '500',
    });
    apiRequest<{ bars: { timestamp: string; close: string; is_synthetic: boolean }[] }>(
      `/market-data/instruments/${instrumentId}/history?${params}`,
    )
      .then((history) => {
        if (cancelled) return;
        const segments = buildSegments(history.bars.filter((bar) => !bar.is_synthetic));
        setState(segments.length ? { status: 'ready', segments } : { status: 'empty' });
      })
      .catch((error: unknown) => {
        if (!cancelled && !isAbortError(error)) setState({ status: 'empty' });
      });
    return () => {
      cancelled = true;
    };
  }, [instrumentId]);
  if (state.status === 'loading') return <Skeleton width="96px" />;
  if (state.status === 'empty') return <small className="trade-subtext">No intraday bars</small>;
  return (
    <svg className="sparkline" width={SPARK_WIDTH} height={SPARK_HEIGHT} viewBox={`0 0 ${SPARK_WIDTH} ${SPARK_HEIGHT}`} role="img" aria-label={`${symbol} intraday close, latest session`}>
      {state.segments.map((segment, index) => segment.length > 1
        ? <polyline key={index} fill="none" strokeWidth="1.4" points={segment.map((point) => `${point.x.toFixed(1)},${point.y.toFixed(1)}`).join(' ')} />
        : <circle key={index} cx={segment[0].x} cy={segment[0].y} r="1.2" />)}
    </svg>
  );
}

// Deterministic templates over backend-computed values. The only operations here are
// selection and counting; no figure is derived, and no model writes any of it.
function buildInsights(
  portfolio: DhanPortfolio,
): string[] {
  const insights: string[] = [];
  const valued = portfolio.holdings.filter((holding) => holding.live_valuation.basis !== BASIS_UNAVAILABLE);
  const moved = valued.filter((holding) => holding.live_valuation.day_change !== null);
  if (moved.length > 0) {
    const largest = moved.reduce((best, holding) =>
      Math.abs(Number(holding.live_valuation.day_change)) > Math.abs(Number(best.live_valuation.day_change)) ? holding : best);
    insights.push(`${largest.symbol} · ${signedMoney(largest.live_valuation.day_change as string)}`);
  }
  if (portfolio.holdings.length > 0) {
    insights.push(`Valued prices · ${valued.length}/${portfolio.holdings.length}`);
  }
  if (portfolio.market_session) {
    insights.push(`Session · ${portfolio.market_session.venue} ${portfolio.market_session.state}`);
  }
  return insights;
}

export function OverviewPage({ workspace }: { workspace: Props }) {
  const [dhanPortfolio, setDhanPortfolio] = useState<DhanPortfolio | null>(null);
  const [brokerSummary, setBrokerSummary] = useState<BrokerPortfolioSummary | null>(null);
  const [dhanLoading, setDhanLoading] = useState(true);
  const [dhanError, setDhanError] = useState<string | null>(null);
  const [alerts, setAlerts] = useState<AlertEvent[] | null>(null);
  const [alertsError, setAlertsError] = useState<string | null>(null);
  const [history, setHistory] = useState<ValuationHistoryResponse | null>(null);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historyError, setHistoryError] = useState<string | null>(null);
  const [historyRange, setHistoryRange] = useState<(typeof HISTORY_RANGES)[number]['label']>('1D');
  const [railTab, setRailTab] = useState<'watchlist' | 'sector' | 'breadth'>('watchlist');
  const [fetchedAt, setFetchedAt] = useState<number | null>(null);
  const [clock, setClock] = useState(() => Date.now());
  const lastPortfolioPushSequence = useRef(0);
  const {
    brokerRefreshKey,
    coverage,
    currency,
    addInstrumentToWatchlist,
    loadingPortfolio,
    portfolioId,
    removeInstrumentFromWatchlist,
    setActiveView,
    setBrokerPriceRows,
    setOpenMarketInstrumentId,
    setCoverageVersion,
    setThesisSymbol,
    summary,
    thesisSymbol,
    valuation,
    valuationBySymbol,
    watchlist,
    watchlistLoadState,
    watchlistLoadError,
    refreshWatchlist,
  } = workspace;

  const refreshDhanData = useCallback(async () => {
    // The server measures each price's age when it responds. Elapsed time is counted from the
    // moment of the request, so a displayed age is never understated.
    const requestedAt = Date.now();
    const [dhan, totals] = await Promise.all([
      apiRequest<DhanPortfolio>('/brokers/dhan/portfolio'),
      apiRequest<BrokerPortfolioSummary>('/brokers/portfolio-summary'),
    ]);
    setDhanPortfolio(dhan);
    setBrokerSummary(totals);
    setFetchedAt(requestedAt);
    setClock(Date.now());
  }, [portfolioId]);

  const { status: dhanFeedStatus, quotes: liveWatchlistQuotes, feedHealth: dhanFeedHealth, portfolioValues, instrumentErrors: liveInstrumentErrors } = useWatchlistStream({
    onError: setDhanError,
  });

  useEffect(() => {
    if (!portfolioValues || portfolioValues.sequence <= lastPortfolioPushSequence.current) return;
    const pushed = portfolioValues;
    lastPortfolioPushSequence.current = pushed.sequence;
    const provenance = pushed.provenance as Partial<Record<PortfolioFigure, FigureProvenance>>;
    const unavailableReasons = pushed.unavailable_reasons as Partial<Record<PortfolioFigure, string>>;
    setBrokerSummary((current) => current ? {
      ...current,
      totals: { ...current.totals, ...pushed.totals },
      brokers: current.brokers.map((broker) => broker.source === 'dhan' ? {
        ...broker,
        provenance,
        unavailable_reasons: unavailableReasons,
      } : broker),
    } : current);
    const rowsByInstrument = new Map(pushed.rows.map((row) => [row.instrument_id, row]));
    const updateRows = <T extends DhanHolding | DhanPosition>(rows: T[]): T[] => rows.map((row) => {
      const update = row.instrument_id ? rowsByInstrument.get(row.instrument_id) : undefined;
      if (!update || Date.parse(update.provider_timestamp) < Date.parse(row.live_valuation.provider_timestamp ?? '')) return row;
      return { ...row, live_valuation: update.valuation as RowValuation };
    });
    setDhanPortfolio((current) => current ? {
      ...current,
      holdings: updateRows(current.holdings),
      positions: updateRows(current.positions),
    } : current);
    setFetchedAt(Date.now());
    setClock(Date.now());
  }, [portfolioValues]);

  useEffect(() => {
    let cancelled = false;
    setDhanLoading(true);
    void refreshDhanData()
      .then(() => {
        if (!cancelled) setDhanError(null);
      })
      .catch((error: unknown) => {
        if (!cancelled) setDhanError(error instanceof Error ? error.message : 'Broker account data is unavailable.');
      })
      .finally(() => {
        if (!cancelled) setDhanLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [refreshDhanData, brokerRefreshKey]);

  useEffect(() => {
    if (!['stale', 'reconnecting', 'unavailable', 'market_closed'].includes(dhanFeedStatus)) return undefined;
    const refreshFallback = () => {
      void refreshDhanData().catch((error: unknown) => {
        setDhanError(error instanceof Error ? error.message : 'Live portfolio valuation is unavailable.');
      });
    };
    refreshFallback();
    const refreshInterval = dhanFeedStatus === 'market_closed' ? 60_000 : 15_000;
    const timer = window.setInterval(refreshFallback, refreshInterval);
    return () => window.clearInterval(timer);
  }, [dhanFeedStatus, refreshDhanData]);

  const refreshAlerts = useCallback(async () => {
    setAlertsError(null);
    try {
      setAlerts(await apiRequest<AlertEvent[]>('/alerts/events?status=new&limit=5'));
    } catch (error) {
      setAlertsError(error instanceof Error ? error.message : 'Alerts could not be loaded.');
    }
  }, []);

  useEffect(() => {
    void refreshAlerts();
  }, [brokerRefreshKey, refreshAlerts]);

  useEffect(() => {
    const timer = window.setInterval(() => void refreshAlerts(), 30_000);
    return () => window.clearInterval(timer);
  }, [refreshAlerts]);

  useEffect(() => {
    const refresh = () => void refreshWatchlist().catch(() => undefined);
    const timer = window.setInterval(refresh, 30_000);
    return () => window.clearInterval(timer);
  }, [refreshWatchlist]);

  useEffect(() => {
    if (!portfolioId) {
      setHistory(null);
      setHistoryLoading(false);
      setHistoryError(null);
      return;
    }
    let cancelled = false;
    setHistory(null);
    setHistoryLoading(true);
    setHistoryError(null);
    apiRequest<ValuationHistoryResponse>(`/portfolios/${portfolioId}/history`)
      .then((response) => {
        if (!cancelled) setHistory(response);
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setHistory(null);
          setHistoryError(error instanceof Error ? error.message : 'Portfolio history is unavailable.');
        }
      })
      .finally(() => {
        if (!cancelled) setHistoryLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [portfolioId, brokerRefreshKey]);

  // The header and sidebar status read these same rows (see useWorkspace.marketDataStatus).
  useEffect(() => {
    if (!dhanPortfolio) return;
    const rows: PriceStateRow[] = [...dhanPortfolio.holdings, ...dhanPortfolio.positions].map((row) => ({
      freshness: resolveRowPrice(row.live_valuation).freshness,
      basis: resolveRowPrice(row.live_valuation).source === 'close'
        ? BASIS_SESSION_CLOSE
        : resolveRowPrice(row.live_valuation).freshness,
      is_synthetic: false,
    }));
    setBrokerPriceRows(rows);
  }, [dhanPortfolio, setBrokerPriceRows]);

  // A delayed price's age is the server's figure plus the time elapsed since the response.
  // Both terms are durations, so an offset between the browser clock and the server clock
  // cannot change what is shown.
  const ageNow: AgeNow = (base) =>
    base === null || base === undefined || fetchedAt === null ? null : base + Math.max(0, clock - fetchedAt) / 1000;
  const hasDelayedRows = [...(dhanPortfolio?.holdings ?? []), ...(dhanPortfolio?.positions ?? [])]
    .some((row) => row.live_valuation.basis === BASIS_DELAYED);

  // While a price is delayed no tick may arrive to trigger a refresh, so re-render on a timer
  // to keep the displayed age growing.
  useEffect(() => {
    if (!hasDelayedRows) return undefined;
    const timer = window.setInterval(() => setClock(Date.now()), 30_000);
    return () => window.clearInterval(timer);
  }, [hasDelayedRows]);

  const instrumentIdForSymbol = (symbol: string) =>
    valuationBySymbol.get(symbol)?.instrument_id
    ?? coverage?.managed.find((entry) => entry.symbol === symbol)?.instrument_id
    ?? coverage?.unmanaged.find((entry) => entry.symbol === symbol)?.instrument_id
    ?? null;
  const openMarketInstrument = (instrumentId: string) => {
    setOpenMarketInstrumentId(instrumentId);
    setActiveView('market-data');
  };

  const dhanBroker = brokerSummary?.brokers.find((broker) => broker.source === 'dhan') ?? brokerSummary?.brokers[0];
  const totals = brokerSummary?.totals ?? null;
  const historyProviderLabel = (row: ValuationHistoryPoint): string => {
    const closeObservation = row.price_observations.find((observation) => observation.price_basis === BASIS_SESSION_CLOSE);
    if (closeObservation) {
      const venue = watchlist.find((item) => item.symbol === closeObservation.symbol)?.venue
        ?? dhanPortfolio?.holdings.find((item) => item.symbol === closeObservation.symbol)?.exchange
        ?? dhanPortfolio?.positions.find((item) => item.symbol === closeObservation.symbol)?.exchange;
      return ` · official close ${sessionCloseLabel(closeObservation.basis_date ?? closeObservation.timestamp, venue)}`;
    }
    return row.provider_timestamp ? ` · provider ${new Date(row.provider_timestamp).toLocaleString()}` : '';
  };
  const reasonFor = (field: PortfolioFigure) =>
    dhanBroker?.unavailable_reasons[field] ?? `No broker snapshot is available for ${FIGURE_LABELS[field].toLowerCase()}.`;

  // Provenance strip: every input comes from the per-row valuation the backend returned.
  const resolvedPortfolioRows = [...(dhanPortfolio?.holdings ?? []), ...(dhanPortfolio?.positions ?? [])]
    .map((row) => ({ row, resolved: resolveRowPrice(row.live_valuation) }))
    .filter((item) => item.resolved.source !== 'unavailable');
  const portfolioPriceItem = resolvedPortfolioRows
    .slice()
    .sort((left, right) => {
      const weakness = (item: typeof left) => BASIS_WEAKNESS[
        item.resolved.source === 'close' ? BASIS_SESSION_CLOSE : item.resolved.freshness
      ] ?? 0;
      return weakness(right) - weakness(left)
        || epochOf(left.resolved.as_of) - epochOf(right.resolved.as_of);
    })[0];
  const portfolioPriceLabel = portfolioPriceItem
    ? resolvedPriceText(
      portfolioPriceItem.resolved,
      delayedAgeText(portfolioPriceItem.resolved.freshness, portfolioPriceItem.row.live_valuation.price_age_seconds, ageNow),
      dhanPortfolio?.market_session?.venue,
    )
    : null;
  const portfolioPriceIsDelayed = portfolioPriceItem?.resolved.source === 'ltp'
    && portfolioPriceItem.resolved.freshness === 'delayed';
  const insights = dhanPortfolio ? buildInsights(dhanPortfolio) : [];
  const historyUsesPortfolio = Boolean(history?.portfolio_scoped.length);
  const historyRows = historyUsesPortfolio
    ? history?.portfolio_scoped ?? []
    : history?.owner_wide ?? [];
  const historyTimes = historyRows.map((row) => Date.parse(row.timestamp)).filter(Number.isFinite);
  const historyStart = historyTimes.length ? Math.min(...historyTimes) : null;
  const historyEnd = historyTimes.length ? Math.max(...historyTimes) : null;
  const selectedHistoryRange = HISTORY_RANGES.find((range) => range.label === historyRange) ?? HISTORY_RANGES[0];
  const historyRangeCovered = (range: (typeof HISTORY_RANGES)[number]) =>
    historyStart !== null && historyEnd !== null && historyEnd - historyStart >= range.milliseconds;
  const chartStart = historyEnd === null ? null : historyEnd - selectedHistoryRange.milliseconds;
  const chartRows = chartStart === null || historyEnd === null
    ? []
    : historyRows.filter((row) => {
      const timestamp = Date.parse(row.timestamp);
      return timestamp >= chartStart && timestamp <= historyEnd;
    });
  const chartValues = chartRows
    .map((row) => row.market_value === null ? null : Number(row.market_value))
    .filter((value): value is number => value !== null && Number.isFinite(value));
  const valueMin = chartValues.length ? Math.min(...chartValues) : 0;
  const valueMax = chartValues.length ? Math.max(...chartValues) : 0;
  const valuePadding = valueMin === valueMax
    ? Math.max(Math.abs(valueMin) * 0.02, 1)
    : (valueMax - valueMin) * 0.08;
  const plotMin = valueMin - valuePadding;
  const plotMax = valueMax + valuePadding;
  const chartSegments: Array<Array<{ row: ValuationHistoryPoint; x: number; y: number; value: number }>> = [];
  let chartSegment: Array<{ row: ValuationHistoryPoint; x: number; y: number; value: number }> = [];
  let previousChartTime: number | null = null;
  for (const row of chartRows) {
    const timestamp = Date.parse(row.timestamp);
    if (row.market_value === null || !Number.isFinite(Number(row.market_value))) {
      if (chartSegment.length) chartSegments.push(chartSegment);
      chartSegment = [];
      previousChartTime = null;
      continue;
    }
    if (previousChartTime !== null && timestamp - previousChartTime > 6 * 60 * 1000) {
      if (chartSegment.length) chartSegments.push(chartSegment);
      chartSegment = [];
    }
    const value = Number(row.market_value);
    chartSegment.push({
      row,
      value,
      x: 52 + ((timestamp - (chartStart ?? timestamp)) / selectedHistoryRange.milliseconds) * 720,
      y: 190 - ((value - plotMin) / (plotMax - plotMin)) * 160,
    });
    previousChartTime = timestamp;
  }
  if (chartSegment.length) chartSegments.push(chartSegment);

  const basisCell = (valued: RowValuation) => {
    const resolved = resolveRowPrice(valued);
    const age = delayedAgeText(resolved.freshness, valued.price_age_seconds, ageNow);
    return resolvedPriceText(resolved, age, dhanPortfolio?.market_session?.venue);
  };

  const holdingsTable = (rows: DhanHolding[]) => (
    <div className="table-scroll"><table>
      <thead><tr>
        <th>Instrument</th><th>Quantity (broker)</th><th>Avg cost (broker)</th><th>LTP</th>
        <th>Market value</th><th>Unrealized P&amp;L</th><th>Day change</th><th>Basis</th>
      </tr></thead>
      <tbody>{rows.map((holding) => {
        const valued = holding.live_valuation;
        const hasPrice = valued.basis !== BASIS_UNAVAILABLE && valued.price !== null;
        return (
          <tr key={`${holding.security_id}-${holding.exchange}`}>
            <td className="instrument-cell">{holding.symbol}<small className="trade-subtext">{holding.exchange}{holding.exchange_resolution_reason ? ` · ${holding.exchange_resolution_reason}` : ''}</small></td>
            <td>{formatQuantity(holding.quantity)}</td>
            <td>{holding.average_price !== null ? money(holding.average_price) : <UnavailableValue reason="The broker did not supply an average cost." />}</td>
            <td>{hasPrice ? money(valued.price as string) : <UnavailableValue reason={valued.stale_reason ?? 'No valued price.'} />}{hasPrice && valued.as_of && <small className="trade-subtext">{istStamp(valued.as_of)}{valued.source ? ` · ${valued.source}` : ''}</small>}</td>
            <td>{valued.market_value !== null ? money(valued.market_value) : '—'}</td>
            <td className={tone(valued.unrealized_profit)}>{valued.unrealized_profit !== null ? signedMoney(valued.unrealized_profit) : '—'}</td>
            <td className={tone(valued.day_change)}>{valued.day_change !== null ? signedMoney(valued.day_change) : '—'}{valued.day_change === null && <small className="trade-subtext">{valued.day_change_reason ?? 'No prior close.'}</small>}</td>
            <td>{basisCell(valued)}</td>
          </tr>
        );
      })}</tbody>
    </table></div>
  );

  const positionsTable = (rows: DhanPosition[]) => (
    <div className="table-scroll"><table>
      <thead><tr>
        <th>Instrument</th><th>Net qty (broker)</th><th>Cost (broker)</th><th>Realized P&amp;L (broker)</th>
        <th>LTP</th><th>Market value</th><th>Unrealized P&amp;L</th><th>Basis</th>
      </tr></thead>
      <tbody>{rows.map((position) => {
        const valued = position.live_valuation;
        const hasPrice = valued.basis !== BASIS_UNAVAILABLE && valued.price !== null;
        return (
          <tr key={`${position.security_id}-${position.exchange}-${position.product}`}>
            <td className="instrument-cell">{position.symbol}<small className="trade-subtext">{position.product || '—'}{position.position_type ? ` · ${position.position_type}` : ''}</small></td>
            <td>{formatQuantity(position.quantity)}</td>
            <td>{position.average_price !== null ? money(position.average_price) : '—'}</td>
            <td className={tone(position.realized_profit)}>{position.realized_profit !== null ? signedMoney(position.realized_profit) : '—'}</td>
            <td>{hasPrice ? money(valued.price as string) : <UnavailableValue reason={valued.stale_reason ?? 'No valued price.'} />}</td>
            <td>{valued.market_value !== null ? money(valued.market_value) : '—'}</td>
            <td className={tone(valued.unrealized_profit)}>{valued.unrealized_profit !== null ? signedMoney(valued.unrealized_profit) : '—'}</td>
            <td>{basisCell(valued)}</td>
          </tr>
        );
      })}</tbody>
    </table></div>
  );

  const watchlistRow = (item: WatchlistItem) => {
    const instrumentError = liveInstrumentErrors[item.instrument_id];
    const quote = instrumentError ? null : liveWatchlistQuotes[item.instrument_id] ?? item.quote;
    const quality = item.relink_required
      ? 'relink required'
      : item.is_synthetic || quote?.is_synthetic
        ? 'unavailable'
        : quote?.freshness_status === 'session_close'
          ? 'closing price'
        : instrumentError ? 'unavailable'
          : quote?.freshness_status.replaceAll('_', ' ') ?? 'unavailable';
    const unusable = item.relink_required || item.is_synthetic || quote?.is_synthetic || !quote;
    return (
      <li
        key={item.instrument_id}
        className="rail-watch-row"
        role="link"
        tabIndex={0}
        onClick={() => openMarketInstrument(item.instrument_id)}
        onKeyDown={(event) => {
          if (event.key === 'Enter' || event.key === ' ') {
            event.preventDefault();
            openMarketInstrument(item.instrument_id);
          }
        }}
        title={`Open ${item.symbol} in Market data`}
      >
        <div className="rail-watch-name"><strong>{item.symbol}</strong><small className="trade-subtext">{item.venue}</small></div>
        <Sparkline instrumentId={item.instrument_id} symbol={item.symbol} />
        <div className="rail-watch-price">
          <span>{quote ? formatMoney(quote.close, item.currency) : <UnavailableValue reason={instrumentError?.reason ?? 'No quote received.'} />}</span>
          <span className={`freshness-badge ${unusable ? 'freshness-unavailable' : `freshness-${quote?.freshness_status}`}`} title={`${quality} · ${instrumentError?.reason ?? quote?.source ?? 'No quote received'}`}>{quality}</span>
          {quote && <small className="trade-subtext">{relativeAge(quote.market_age_seconds)}</small>}
        </div>
        <button className="text-button watchlist-row-action" type="button" onClick={(event) => { event.stopPropagation(); void removeInstrumentFromWatchlist(item.instrument_id); }}>Remove</button>
      </li>
    );
  };

  return (
    <div className="overview-page">
      <div className="overview-heading">
        <h1 className="page-title">Overview</h1>
        <p className="muted">Decision support, not investment advice. Brokers are read-only.</p>
      </div>
      {dhanError && <p className="broker-sync-time" role="alert">{dhanError}</p>}

      <section className="metrics-row portfolio-metrics" aria-label="Portfolio metrics">
        <MetricCard
          label="Portfolio Value"
          value={totals?.market_value ?? null}
          loading={dhanLoading}
          reason={reasonFor('market_value')}
          subline={totals?.invested_cost != null ? `Invested ${money(totals.invested_cost)}` : undefined}
          basis={cardBasis(dhanBroker, 'market_value', ageNow, dhanPortfolio?.market_session?.venue, portfolioPriceLabel ?? undefined)}
        />
        <MetricCard
          label="Unrealized P&L"
          value={totals?.unrealized_pnl ?? null}
          loading={dhanLoading}
          reason={reasonFor('unrealized_pnl')}
          basis={cardBasis(dhanBroker, 'unrealized_pnl', ageNow, dhanPortfolio?.market_session?.venue, portfolioPriceLabel ?? undefined)}
          signed
        />
        <MetricCard
          label="Day Change"
          value={totals?.day_change ?? null}
          loading={dhanLoading}
          reason={reasonFor('day_change')}
          subline={dhanBroker?.unavailable_reasons.day_change ?? 'Complete across all holdings and positions'}
          basis={cardBasis(dhanBroker, 'day_change', ageNow, dhanPortfolio?.market_session?.venue, portfolioPriceLabel ?? undefined)}
          signed
        />
        <MetricCard
          label="Cash Balance"
          value={totals?.cash ?? null}
          loading={dhanLoading}
          reason={reasonFor('cash')}
          basis={cardBasis(dhanBroker, 'cash', ageNow, dhanPortfolio?.market_session?.venue)}
        />
      </section>

      <div className={`provenance-strip${portfolioPriceIsDelayed ? ' provenance-strip-delayed' : ''}`} role="status" aria-label="Price provenance">
        {dhanLoading
          ? <Skeleton width="24rem" />
          : portfolioPriceLabel
            ? <>
                <strong>{portfolioPriceLabel}</strong>
              </>
            : <span>No valued price is available, so no valuation timestamp can be shown.</span>}
      </div>
      {!!brokerSummary?.brokers.length && <EvidenceList brokers={brokerSummary.brokers} ageNow={ageNow} venue={dhanPortfolio?.market_session?.venue} />}
      {brokerSummary?.value_comparison?.alert && brokerSummary.value_comparison.app_value !== null && brokerSummary.value_comparison.broker_value !== null && brokerSummary.value_comparison.difference !== null && <p className="broker-sync-time" role="alert">
        App valuation {money(brokerSummary.value_comparison.app_value)} as of {brokerSummary.value_comparison.app_timestamp ? istStamp(brokerSummary.value_comparison.app_timestamp) : 'unknown'} differs from Dhan-derived {money(brokerSummary.value_comparison.broker_value)} as of {brokerSummary.value_comparison.broker_timestamp ? istStamp(brokerSummary.value_comparison.broker_timestamp) : 'unknown'} by {signedMoney(brokerSummary.value_comparison.difference)} (alert threshold {money(brokerSummary.value_comparison.threshold)}).
      </p>}

      <div className="overview-grid">
        <div className="overview-main">
          <section className="data-section" aria-label="Broker sync health">
            <div className="section-heading"><div><p className="eyebrow">READ-ONLY BROKER DATA · INR</p><h2>Broker sync health</h2></div></div>
            {dhanLoading ? <Skeleton width="16rem" /> : !dhanPortfolio ? <div className="table-empty">Dhan data is unavailable.</div> : (
              <dl className="sync-health">
                <div><dt>Last synced</dt><dd>{dhanPortfolio.synced_at ? new Date(dhanPortfolio.synced_at).toLocaleString() : 'Never'}</dd></div>
                <div><dt>Live feed</dt><dd>{dhanFeedStatus === 'active' ? 'Live' : dhanFeedStatus === 'stale' ? 'Stale · no recent packet' : dhanFeedStatus === 'market_closed' ? 'Idle · market closed' : dhanFeedStatus}{typeof dhanFeedHealth?.consecutive_failure_count === 'number' ? ` · consecutive failures ${dhanFeedHealth.consecutive_failure_count}` : ''}{typeof dhanFeedHealth?.last_failure_reason === 'string' ? ` · last failure ${dhanFeedHealth.last_failure_reason}${typeof dhanFeedHealth.last_failure_at === 'string' ? ` at ${new Date(dhanFeedHealth.last_failure_at).toLocaleTimeString()}` : ''}` : ''}</dd></div>
                <div><dt>Instrument master</dt><dd>{dhanPortfolio.instrument_master.last_refreshed_at ? new Date(dhanPortfolio.instrument_master.last_refreshed_at).toLocaleString() : 'Not loaded in this process'}</dd></div>
                <div><dt>Snapshot cadence</dt><dd>every {Math.max(30, dhanPortfolio.snapshot_min_interval_seconds)}s while open</dd></div>
              </dl>
            )}
            {!!dhanPortfolio?.instrument_master.mapping_candidates.length && (
              <details className="portfolio-evidence portfolio-evidence-card">
                <summary>Instrument mapping review · {dhanPortfolio.instrument_master.mapping_candidates.length} candidates</summary>
                <ul>
                  {dhanPortfolio.instrument_master.mapping_candidates.map((candidate, index) => (
                    <li key={`${candidate.security_id ?? candidate.isin ?? candidate.symbol}-${index}`}>
                      <strong>{candidate.symbol}</strong>
                      {candidate.security_id && ` · Security ID ${candidate.security_id}`}
                      {candidate.isin && ` · ISIN ${candidate.isin}`}
                      {candidate.segment && ` · ${candidate.segment}`}
                      {` · ${candidate.status}`}
                      {candidate.reason && ` · ${candidate.reason}`}
                      {candidate.verification_required && ` · ${candidate.verification_required}`}
                    </li>
                  ))}
                </ul>
              </details>
            )}
          </section>

          <section className="data-section" aria-label="Holdings">
            <div className="section-heading"><div><h2>Holdings</h2><small className="trade-subtext">Quantity and average cost are broker-sourced. Price, value, P&amp;L and day change come from the valuation service.</small></div><span className="section-count">{dhanPortfolio ? `${dhanPortfolio.holdings.length} records` : ''}</span></div>
            {dhanLoading ? <Skeleton width="100%" /> : !dhanPortfolio?.holdings.length ? <div className="table-empty">No Dhan holdings have been synced.</div> : holdingsTable(dhanPortfolio.holdings)}
          </section>

          <section className="data-section" aria-label="Open positions">
            <div className="section-heading"><div><h2>Open positions</h2></div><span className="section-count">{dhanPortfolio ? `${dhanPortfolio.positions.length} records` : ''}</span></div>
            {dhanLoading ? <Skeleton width="100%" /> : !dhanPortfolio?.positions.length ? <div className="table-empty">No Dhan open positions have been synced.</div> : positionsTable(dhanPortfolio.positions)}
            {!!dhanPortfolio?.closed_positions.length && <details className="closed-positions-details">
              <summary>Closed today · {dhanPortfolio.closed_positions.length} records · realized P&amp;L retained</summary>
              <div className="table-scroll"><table><thead><tr><th>Instrument</th><th>Exchange</th><th>Product / type</th><th>Net quantity</th><th>Realized P&amp;L (Dhan)</th></tr></thead><tbody>{dhanPortfolio.closed_positions.map((position, index) => <tr key={`${position.security_id}-${position.exchange}-${position.product}-${index}`}><td className="instrument-cell">{position.symbol}</td><td>{position.exchange}</td><td>{position.product || '—'}{position.position_type ? ` · ${position.position_type}` : ''}</td><td>{formatQuantity(position.quantity)}</td><td className={tone(position.realized_profit)}>{position.realized_profit !== null ? signedMoney(position.realized_profit) : '—'}</td></tr>)}</tbody></table></div>
            </details>}
            {!!dhanPortfolio?.delivery_sales.length && <details className="closed-positions-details">
              <summary>CNC sales from delivery holdings · {dhanPortfolio.delivery_sales.length} records</summary>
              <div className="table-scroll"><table><thead><tr><th>Instrument</th><th>Net quantity sold</th><th>Average price</th></tr></thead><tbody>{dhanPortfolio.delivery_sales.map((sale, index) => <tr key={`${sale.security_id}-${sale.exchange}-${index}`}><td className="instrument-cell">{sale.symbol}</td><td>{formatQuantity(sale.quantity)}</td><td>{sale.average_price !== null ? money(sale.average_price) : '—'}</td></tr>)}</tbody></table></div>
            </details>}
          </section>

          <section className="data-section" aria-label="Alerts">
            <div className="section-heading"><div><h2>Alerts</h2><small className="trade-subtext">Decision support, not investment advice.</small></div><button className="text-button" type="button" onClick={() => setActiveView('alerts')}>Open alerts</button></div>
            {alerts === null && !alertsError ? <Skeleton width="12rem" /> : alertsError ? <ErrorState title="Alerts unavailable" message={alertsError} onRetry={() => void refreshAlerts()} /> : alerts?.length === 0 ? <div className="table-empty">No new alerts.</div> : alerts && (
              <ul className="overview-alerts">
                {alerts.map((event) => (
                  <li key={event.id}><span className={`freshness-badge freshness-${event.severity === 'critical' ? 'unavailable' : 'fresh'}`}>{event.severity}</span> {event.rule_type.replaceAll('_', ' ')} <small className="trade-subtext">{new Date(event.fired_at).toLocaleString()}</small></li>
                ))}
              </ul>
            )}
          </section>

          <section className="data-section" aria-label="Key insights">
            <div className="section-heading"><div><h2>Key insights</h2><small className="trade-subtext">Fixed templates over computed values. Decision support, not investment advice.</small></div></div>
            {dhanLoading ? <Skeleton width="20rem" /> : insights.length === 0 ? <div className="table-empty">No computed insights are available yet.</div> : (
              <ul className="insights">{insights.map((line) => <li key={line}>{line}</li>)}</ul>
            )}
          </section>

          <section className="data-section history-panel" aria-label="Portfolio value history">
            <div className="section-heading">
              <div>
                <h2>Portfolio value history</h2>
                <small className="trade-subtext">
                  {historyUsesPortfolio ? 'Selected portfolio' : 'Owner-wide · all broker holdings'}
                  {' · decision support, not investment advice'}
                </small>
              </div>
              {historyRows.length > 0 && (
                <div className="history-ranges" aria-label="History range">
                  {HISTORY_RANGES.map((range) => (
                    <button
                      className={historyRange === range.label ? 'history-range active' : 'history-range'}
                      disabled={!historyRangeCovered(range)}
                      key={range.label}
                      onClick={() => setHistoryRange(range.label)}
                      type="button"
                    >{range.label}</button>
                  ))}
                </div>
              )}
            </div>
            {historyLoading ? <Skeleton width="100%" /> : historyError ? (
              <div className="table-empty" role="status">{historyError}</div>
            ) : historyRows.length === 0 ? (
              <div className="table-empty">Collecting - first data at the next market open.</div>
            ) : (
              <>
                {!historyUsesPortfolio && <p className="history-scope-note">Showing owner-wide history from stored portfolio snapshots.</p>}
                {chartValues.length === 0 ? (
                  <div className="table-empty">No complete market-value points are available in this range.</div>
                ) : (
                  <>
                    <div className="history-chart-wrap">
                      <div className="history-chart-scale" aria-hidden="true">
                        <span>{formatMoney(String(valueMax), historyRows[0].currency)}</span>
                        <span>{formatMoney(String(valueMin), historyRows[0].currency)}</span>
                      </div>
                      <svg
                        className="history-chart"
                        viewBox="0 0 800 220"
                        role="img"
                        aria-label={`Stored portfolio market value history, ${historyUsesPortfolio ? 'selected portfolio' : 'owner-wide all broker holdings'}`}
                      >
                        {[30, 110, 190].map((y) => <line className="history-gridline" key={y} x1="52" x2="772" y1={y} y2={y} />)}
                        {chartSegments.map((segment, index) => (
                          <g key={`history-segment-${index}`}>
                            {segment.length > 1 && <polyline className="history-line" points={segment.map((point) => `${point.x},${point.y}`).join(' ')} />}
                            {segment.map((point) => (
                              <circle className="history-point" cx={point.x} cy={point.y} key={point.row.timestamp} r="3">
                                <title>
                                  {`${new Date(point.row.timestamp).toLocaleString()} · ${formatMoney(String(point.value), point.row.currency)} · ${point.row.valuation_basis} · ${point.row.source}${historyProviderLabel(point.row)}${point.row.complete ? '' : ` · partial: ${point.row.reason ?? 'incomplete valuation'}`}`}
                                </title>
                              </circle>
                            ))}
                          </g>
                        ))}
                      </svg>
                    </div>
                    <div className="history-chart-caption">
                      {chartRows.length} stored observations · gaps over 6 minutes are not connected.
                      {!HISTORY_RANGES.some(historyRangeCovered) && ' A range becomes available when stored data covers it.'}
                    </div>
                  </>
                )}
              </>
            )}
          </section>

          {!loadingPortfolio && summary && (
            <details className="data-section overview-collapsed">
              <summary>Manual ledger · separate from broker totals · {summary.positions.length} positions</summary>
              {summary.positions.length === 0 ? <div className="table-empty">No open positions recorded.</div> : (
                <div className="table-scroll"><table><thead><tr><th>Instrument</th><th>Quantity</th><th>Held since</th><th>Average cost</th><th>Cost basis</th><th>Last price</th><th>Market value</th><th>Unrealized P&amp;L</th><th>Day change</th><th>Weight</th><th>Data quality</th><th>Thesis</th><th>Watchlist</th></tr></thead><tbody>{summary.positions.map((position) => {
                  const priced = valuationBySymbol.get(position.symbol);
                  const needsAttention = !priced || priced.freshness === 'stale' || priced.is_synthetic || priced.freshness === 'unavailable';
                  const isWatchlisted = Boolean(priced?.instrument_id && watchlist.some((item) => item.instrument_id === priced.instrument_id));
                  const managed = coverage?.managed.find((entry) => entry.symbol === position.symbol);
                  return <tr key={position.symbol}>
                    <td className="instrument-cell">{position.symbol}</td>
                    <td>{formatQuantity(position.quantity)}</td>
                    <td>{position.holding_period_start ? new Date(position.holding_period_start).toLocaleDateString() : '—'}</td>
                    <td>{formatMoney(position.average_cost, currency)}</td>
                    <td>{formatMoney(position.cost_basis, currency)}</td>
                    <td>{priced?.last_price ? formatMoney(priced.last_price, currency) : '—'}<small className="trade-subtext">{priced ? priced.basis === BASIS_SESSION_CLOSE ? `Official close · ${sessionCloseLabel(priced.provider_timestamp, watchlist.find((item) => item.instrument_id === priced.instrument_id)?.venue)} · fetched ${priced.retrieved_at ? new Date(priced.retrieved_at).toLocaleString() : 'time unavailable'}` : `${basisCaption(priced.basis)}${priced.provider_timestamp ? ` · ${new Date(priced.provider_timestamp).toLocaleString()}` : ''}` : 'Not valued'}</small></td>
                    <td>{priced?.market_value ? formatMoney(priced.market_value, currency) : '—'}</td>
                    <td className={priced?.unrealized_pnl ? tone(priced.unrealized_pnl) : undefined}>{priced?.unrealized_pnl ? formatMoney(priced.unrealized_pnl, currency) : '—'}</td>
                    <td>{priced?.day_change ? formatMoney(priced.day_change, currency) : '—'}{priced?.day_change_percent ? ` (${Number(priced.day_change_percent).toFixed(2)}%)` : ''}{priced && !priced.day_change ? <small className="trade-subtext">{priced.day_change_reason ?? 'No prior close'}</small> : null}</td>
                    <td>{priced?.weight_percent ? `${Number(priced.weight_percent).toFixed(2)}%` : '—'}</td>
                    <td>{needsAttention ? <span className={`freshness-badge freshness-${priced?.is_synthetic ? 'unavailable' : priced?.freshness ?? 'unavailable'}`}>{priced?.is_synthetic ? 'unavailable' : priced?.freshness ?? 'unlinked'}</span> : <span className="freshness-badge freshness-fresh">verified · recent</span>}</td>
                    <td><button className="secondary-button thesis-button" type="button" onClick={() => setThesisSymbol(position.symbol)}>{coverage === null ? 'Thesis' : managed ? (managed.review_overdue ? 'Review due' : 'Edit thesis') : 'Unmanaged · add thesis'}</button></td>
                    <td>{priced?.instrument_id && <button className="text-button watchlist-row-action" type="button" onClick={() => void (isWatchlisted ? removeInstrumentFromWatchlist(priced.instrument_id!) : addInstrumentToWatchlist(priced.instrument_id!, position.symbol))}>{isWatchlisted ? 'Remove' : 'Add'}</button>}</td>
                  </tr>;
                })}</tbody></table></div>
              )}
              {coverage && coverage.unmanaged.length > 0 && <section className="unmanaged-positions" aria-label="Unmanaged positions">
                <h3>Unmanaged positions</h3>
                {coverage.unmanaged.map((entry) => <article className="unmanaged-position" key={entry.symbol}>
                  <div><strong>{entry.symbol}</strong><span>{entry.reason === 'no_instrument_linked' ? 'Link this holding to a market instrument before creating its thesis.' : entry.reason === 'thesis_not_active' ? `Most recent thesis is ${entry.thesis_status ?? 'not active'}; create or reactivate an active thesis.` : 'No active thesis exists for this holding.'}</span></div>
                  <button className="secondary-button" type="button" onClick={() => setThesisSymbol(entry.symbol)}>{entry.instrument_id ? 'Open thesis editor' : 'Link instrument / thesis'}</button>
                </article>)}
              </section>}
              {thesisSymbol && portfolioId && <ThesisEditor portfolioId={portfolioId} currency={currency} symbol={thesisSymbol} instrumentId={instrumentIdForSymbol(thesisSymbol)} onClose={() => setThesisSymbol(null)} onChanged={() => setCoverageVersion((value) => value + 1)} />}
              <div className="valuation-footnote">{valuation?.confidence === 'unreliable' ? `Valuation is unreliable: ${valuation.reasons.join(' ')}` : `Open cost basis ${formatMoney(summary.invested_cost, currency)} · market prices are not FX-converted.`}</div>
              <TransactionEntry {...workspace} />
            </details>
          )}
        </div>

        <aside className="overview-rail" aria-label="Market rail">
          <div className="rail-tabs" role="tablist" aria-label="Market rail">
            {(['watchlist', 'sector', 'breadth'] as const).map((tab) => (
              <button key={tab} type="button" role="tab" aria-selected={railTab === tab} className={railTab === tab ? 'rail-tab active' : 'rail-tab'} onClick={() => setRailTab(tab)}>{tab === 'watchlist' ? 'Watchlist' : tab === 'sector' ? 'Sector' : 'Breadth'}</button>
            ))}
          </div>
          <div className="data-section rail-panel" role="tabpanel">
            {railTab === 'watchlist' && (watchlistLoadState === 'loading'
              ? <Skeleton width="12rem" />
              : watchlistLoadState === 'failed'
                ? <ErrorState title="Watchlist unavailable" message={watchlistLoadError} onRetry={() => void refreshWatchlist(true).catch(() => undefined)} />
                : watchlist.length === 0
                  ? <div className="table-empty">No instruments on your watchlist. Add one from Markets.</div>
                  : <ul className="rail-watchlist">{watchlist.map(watchlistRow)}</ul>)}
            {railTab === 'sector' && <div className="table-empty">No sector data source is connected, so no sector view is shown.</div>}
            {railTab === 'breadth' && <div className="table-empty">Market breadth has no data source and is not shown.</div>}
            {railTab === 'watchlist' && <div className="valuation-footnote">Sparklines are stored 5-minute bars of the latest session. Gaps are left as gaps.</div>}
          </div>
        </aside>
      </div>
    </div>
  );
}
