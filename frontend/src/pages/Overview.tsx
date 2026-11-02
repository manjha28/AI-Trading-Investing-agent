import { useCallback, useEffect, useState } from 'react';
import { Info } from 'lucide-react';
import { apiRequest } from '../api';
import { formatMoney, formatQuantity, type WorkspaceState } from '../components/useWorkspace';
import { TransactionEntry } from '../components/TransactionEntry';
import { ThesisEditor } from '../components/ThesisEditor';

type Props = Pick<
  WorkspaceState,
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
>;

type LiveValuation = {
  status: 'live' | 'stale';
  basis: 'live_feed_ltp';
  market_value: string | null;
  unrealized_profit: string | null;
  quote_at: string | null;
  stale_reason: string | null;
};

type LiveWatchlistQuote = {
  instrument_id: string;
  security_id: string | null;
  close: string;
  source: string;
  market_timestamp: string;
  market_age_seconds: number;
  freshness_status: string;
  is_synthetic: boolean;
  is_verified: boolean;
};

type DhanPortfolio = {
  source: 'dhan';
  currency: 'INR';
  synced_at: string | null;
  snapshot_min_interval_seconds: number;
  instrument_master: {
    last_refreshed_at: string | null;
    mapping_candidates: {
      symbol: string;
      security_id: string | null;
      isin: string | null;
      provider_instrument_id: string | null;
      segment: string | null;
      listing_date: string | null;
      status: string;
      reason: string | null;
      verification_required: string | null;
    }[];
  };
  holdings: {
    symbol: string;
    exchange: string;
    exchange_raw: string;
    exchange_resolution_reason: string | null;
    product: string;
    security_id: string | null;
    instrument_id: string | null;
    isin: string | null;
    quantity: string;
    average_price: string | null;
    live_valuation: LiveValuation;
    synced_at: string;
  }[];
  positions: {
    symbol: string;
    exchange: string;
    exchange_raw: string;
    exchange_resolution_reason: string | null;
    product: string;
    security_id: string | null;
    instrument_id: string | null;
    quantity: string;
    average_price: string | null;
    position_type: string | null;
    realized_profit: string | null;
    unrealized_profit: string | null;
    broker_unrealized_profit_basis: 'broker_snapshot';
    broker_snapshot_at: string;
    live_valuation: LiveValuation;
    synced_at: string;
  }[];
  closed_positions: {
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
  }[];
  delivery_sales: {
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
  }[];
  funds: {
    available_balance: string;
    sod_limit: string | null;
    collateral_amount: string | null;
    receivable_amount: string | null;
    utilized_amount: string | null;
    blocked_payout_amount: string | null;
    withdrawable_balance: string | null;
    synced_at: string;
  } | null;
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
  price_basis?: 'live_quote' | 'eod_close';
  basis_date?: string;
  provider_attempts?: string[];
  quantity?: string;
  ltp?: string;
  close?: string;
  average_cost?: string | null;
  unrealized_profit?: string | null;
  amount?: string;
};

type FigureProvenance = {
  type: 'broker_supplied' | 'derived' | 'combined';
  description: string;
  quote_observations?: EvidenceObservation[];
  previous_close_observations?: EvidenceObservation[];
  mirror_observations?: EvidenceObservation[];
  exclusions?: string[];
  coverage?: {
    priced: number;
    total: number;
    unit: string;
  };
};

type BrokerPortfolioSummary = {
  currency: 'INR';
  source: string | null;
  sources: string[];
  synced_at: string | null;
  totals: {
    cash: string | null;
    invested_cost: string | null;
    positions_exposure: string | null;
    holdings_unrealized_pnl: string | null;
    positions_unrealized_pnl: string | null;
    market_value: string | null;
    total_value: string | null;
    unrealized_pnl: string | null;
    day_change: string | null;
    synced_at: string | null;
  };
  brokers: {
    source: string;
    currency: 'INR';
    synced_at: string | null;
    cash: string | null;
    invested_cost: string | null;
    positions_exposure: string | null;
    holdings_unrealized_pnl: string | null;
    positions_unrealized_pnl: string | null;
    market_value: string | null;
    total_value: string | null;
    unrealized_pnl: string | null;
    day_change: string | null;
    holdings_count: number;
    positions_count: number;
    unavailable_reasons: Partial<Record<PortfolioFigure, string>>;
    provenance: Partial<Record<PortfolioFigure, FigureProvenance>>;
  }[];
};

function portfolioFigureLabel(field: PortfolioFigure): string {
  return {
    cash: 'Cash balance',
    invested_cost: 'Invested cost (delivery holdings)',
    positions_exposure: 'Positions exposure (cost basis)',
    holdings_unrealized_pnl: 'Holdings unrealized P&L',
    positions_unrealized_pnl: 'Positions unrealized P&L',
    market_value: 'Market value (holdings + positions)',
    total_value: 'Total value (cash + holdings + positions)',
    unrealized_pnl: 'Unrealized P&L (holdings + positions)',
    day_change: 'Day change (holdings + positions)',
  }[field];
}

function provenanceDate(date: string | undefined): string | null {
  if (!date) return null;
  const [year, month, day] = date.split('-').map(Number);
  return new Intl.DateTimeFormat(undefined, {
    day: 'numeric',
    month: 'short',
  }).format(new Date(year, month - 1, day));
}

function provenanceCaption(
  broker: BrokerPortfolioSummary['brokers'][number],
  field: PortfolioFigure,
): string {
  const provenance = broker.provenance[field];
  if (!provenance) return 'Source details unavailable.';
  const label = provenance.type === 'broker_supplied'
    ? 'Broker-supplied'
    : provenance.type === 'combined'
      ? 'Combined'
      : 'Derived';
  const observations = [
    ...(provenance.quote_observations ?? []),
    ...(provenance.previous_close_observations ?? []),
    ...(provenance.mirror_observations ?? []),
  ];
  const sources = [...new Set(observations.map((observation) => observation.source))];
  if (provenance.type === 'broker_supplied') {
    const timestamp = observations.find((observation) => observation.timestamp)?.timestamp;
    return `${label} · ${broker.source}${timestamp ? ` · ${new Date(timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}` : ''}`;
  }
  const coverage = provenance.coverage;
  const coverageText = coverage
    ? `${coverage.priced} of ${coverage.total} ${coverage.unit} priced${coverage.total > coverage.priced ? ` · ${coverage.total - coverage.priced} excluded` : ''}`
    : null;
  const quoteObservations = provenance.quote_observations ?? [];
  const eodObservations = quoteObservations.filter(
    (observation) => observation.price_basis === 'eod_close',
  );
  const basis = eodObservations.length > 0
    ? field === 'day_change'
      ? `At close · ${provenanceDate(provenance.previous_close_observations?.[0]?.basis_date) ?? 'prior session'} → ${provenanceDate(eodObservations[0].basis_date) ?? 'last session'}`
      : `At close · ${provenanceDate(eodObservations[0].basis_date) ?? 'last session'}`
    : quoteObservations.length > 0
      ? 'Live quote'
      : null;
  return `${label}${basis ? ` · ${basis}` : ''}${sources.length ? ` · ${sources.join(', ')}` : ''}${coverageText ? ` · ${coverageText}` : ''}`;
}

function compactBasis(
  broker: BrokerPortfolioSummary['brokers'][number],
  field: PortfolioFigure,
): string {
  const provenance = broker.provenance[field];
  if (!provenance) return '';
  const observations = [
    ...(provenance.quote_observations ?? []),
    ...(provenance.previous_close_observations ?? []),
    ...(provenance.mirror_observations ?? []),
  ];
  const eod = observations.find((observation) => observation.price_basis === 'eod_close');
  if (eod) return `At close · ${provenanceDate(eod.basis_date) ?? 'last session'}`;
  const timestamp = observations.find((observation) => observation.timestamp)?.timestamp;
  const time = timestamp
    ? new Date(timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
    : '';
  if (provenance.type === 'broker_supplied') return `Broker${time ? ` · ${time}` : ''}`;
  if (provenance.quote_observations?.length) return `Live${time ? ` · ${time}` : ''}`;
  return provenance.type === 'combined' ? 'Combined' : '';
}

function UnavailableValue({ reason }: { reason: string }) {
  return (
    <span className="portfolio-unavailable" title={reason} aria-label={`Not available: ${reason}`}>
      <Info size={13} aria-hidden="true" />
      <span>Not available</span>
    </span>
  );
}

function ProvenanceDetails({
  broker,
  field,
}: {
  broker: BrokerPortfolioSummary['brokers'][number];
  field: PortfolioFigure;
}) {
  const provenance = broker.provenance[field];
  const observations = provenance
    ? [
        ...(provenance.quote_observations ?? []),
        ...(provenance.previous_close_observations ?? []),
        ...(provenance.mirror_observations ?? []),
      ]
    : [];
  const reason = broker.unavailable_reasons[field];
  const exclusions = (provenance?.exclusions ?? [])
    .map((item) => item.replace(/^Missing input:\s*/, '').trim())
    .filter(Boolean);
  const normalizedReason = reason?.replace(/^Missing input:\s*/, '').trim();
  const reasonParts = normalizedReason
    ? exclusions.includes(normalizedReason)
      ? []
      : normalizedReason.split("; ").filter(Boolean)
    : [];
  const detailReasons = [...new Set([...exclusions, ...reasonParts])];
  return (
    <section
      className="portfolio-evidence"
      style={{ fontSize: '0.72rem', lineHeight: 1.25, marginTop: '0.15rem' }}
    >
      <h4>{portfolioFigureLabel(field)}</h4>
      <p style={{ fontSize: '0.72rem', lineHeight: 1.25, margin: '0.2rem 0' }}>
        {provenanceCaption(broker, field)}
      </p>
      {provenance && <p style={{ fontSize: '0.72rem', lineHeight: 1.25, margin: '0.2rem 0' }}>{provenance.description}</p>}
      {observations.length > 0 && <ul style={{ fontSize: '0.72rem', lineHeight: 1.25, margin: '0.2rem 0', paddingLeft: '1rem' }}>
        {observations.map((observation, index) => {
          const inputs = [
            observation.quantity !== undefined
              ? `quantity ${formatQuantity(observation.quantity)}`
              : null,
            observation.ltp !== undefined
              ? `${observation.price_basis === 'eod_close' ? `At close${provenanceDate(observation.basis_date) ? ` · ${provenanceDate(observation.basis_date)}` : ''}` : 'LTP'} ${formatMoney(observation.ltp, broker.currency)}`
              : null,
            observation.close !== undefined
              ? `close ${formatMoney(observation.close, broker.currency)}`
              : null,
            observation.average_cost
              ? `average cost ${formatMoney(observation.average_cost, broker.currency)}`
              : null,
            observation.unrealized_profit
              ? `Dhan unrealizedProfit ${formatMoney(observation.unrealized_profit, broker.currency)}`
              : null,
            observation.amount
              ? `balance ${formatMoney(observation.amount, broker.currency)}`
              : null,
          ].filter(Boolean);
          return <li
            key={`${observation.symbol}-${observation.source}-${index}`}
            style={{ whiteSpace: 'nowrap', overflowX: 'auto' }}
          >
            {observation.symbol} · {observation.source}
            {observation.timestamp ? ` · ${new Date(observation.timestamp).toLocaleString()}` : ''}
            {inputs.length ? ` · ${inputs.join(' · ')}` : ''}
          </li>;
        })}
      </ul>}
      {detailReasons.length > 0 ? <ul style={{ fontSize: '0.72rem', lineHeight: 1.25, margin: '0.2rem 0', paddingLeft: '1rem' }}>
        {detailReasons.map((exclusion, index) => (
          <li key={`${index}-${exclusion}`}>{exclusion}</li>
        ))}
      </ul> : null}
      {observations.some((observation) => observation.provider_attempts?.length) && (
        <ul className="portfolio-provider-attempts">
          {[...new Set(observations.flatMap((observation) => observation.provider_attempts ?? []))]
            .map((attempt, index) => <li key={`${index}-${attempt}`}>{attempt}</li>)}
        </ul>
      )}
    </section>
  );
}

function PortfolioEvidence({ brokers }: { brokers: BrokerPortfolioSummary['brokers'] }) {
  const fields: PortfolioFigure[] = [
    'cash',
    'invested_cost',
    'positions_exposure',
    'holdings_unrealized_pnl',
    'positions_unrealized_pnl',
    'market_value',
    'total_value',
    'unrealized_pnl',
    'day_change',
  ];
  return (
    <details className="portfolio-evidence portfolio-evidence-card">
      <summary>Evidence</summary>
      <p className="portfolio-evidence-note">
        Cash and open-position P&amp;L are broker-supplied; market values use a live quote in session or the last completed session close when shut. Day change uses the two session dates. INR only; no FX conversion.
      </p>
      {brokers.flatMap((broker) => fields.map((field) => (
        <ProvenanceDetails key={`${broker.source}-${field}`} broker={broker} field={field} />
      )))}
    </details>
  );
}

function relativeAge(seconds: number): string {
  if (seconds < 60) return 'Just now';
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 48 * 3600) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

function nextNseOpenDelayMs(): number {
  const now = new Date();
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Kolkata',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).formatToParts(now);
  const part = (type: string) => parts.find((item) => item.type === type)?.value ?? '';
  let nextOpen = Date.UTC(
    Number(part('year')),
    Number(part('month')) - 1,
    Number(part('day')),
    3,
    45,
  );
  if (nextOpen <= now.getTime()) {
    const tomorrow = new Date(nextOpen);
    tomorrow.setUTCDate(tomorrow.getUTCDate() + 1);
    nextOpen = tomorrow.getTime();
  }
  while ([0, 6].includes(new Date(nextOpen).getUTCDay())) {
    const nextWeekday = new Date(nextOpen);
    nextWeekday.setUTCDate(nextWeekday.getUTCDate() + 1);
    nextOpen = nextWeekday.getTime();
  }
  return Math.max(1000, nextOpen - now.getTime());
}

export function OverviewPage({ workspace }: { workspace: Props }) {
  const [dhanPortfolio, setDhanPortfolio] = useState<DhanPortfolio | null>(null);
  const [brokerSummary, setBrokerSummary] = useState<BrokerPortfolioSummary | null>(null);
  const [dhanLoading, setDhanLoading] = useState(true);
  const [dhanSyncing, setDhanSyncing] = useState(false);
  const [dhanError, setDhanError] = useState<string | null>(null);
  const [dhanFeedStatus, setDhanFeedStatus] = useState('connecting');
  const [liveWatchlistQuotes, setLiveWatchlistQuotes] = useState<Record<string, LiveWatchlistQuote>>({});
  const {
    coverage,
    currency,
    addInstrumentToWatchlist,
    loadingPortfolio,
    marketDataStatus,
    portfolioId,
    removeInstrumentFromWatchlist,
    setActiveView,
    setOpenMarketInstrumentId,
    setCoverageVersion,
    setThesisSymbol,
    summary,
    thesisSymbol,
    valuation,
    valuationBySymbol,
    watchlist,
  } = workspace;
  const refreshDhanPortfolio = useCallback(async () => {
    setDhanPortfolio(await apiRequest<DhanPortfolio>('/brokers/dhan/portfolio'));
  }, []);

  const refreshDhanData = useCallback(async () => {
      const [dhan, summary] = await Promise.all([
      apiRequest<DhanPortfolio>('/brokers/dhan/portfolio'),
      apiRequest<BrokerPortfolioSummary>('/brokers/portfolio-summary'),
    ]);
    setDhanPortfolio(dhan);
    setBrokerSummary(summary);
  }, []);

  useEffect(() => {
    let cancelled = false;
    void refreshDhanData()
      .catch((error: unknown) => {
        if (!cancelled) {
          setDhanError(error instanceof Error ? error.message : 'Broker account data is unavailable.');
        }
      })
      .finally(() => {
        if (!cancelled) setDhanLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [refreshDhanData]);

  useEffect(() => {
    let cancelled = false;
    let socket: WebSocket | null = null;
    let reconnectTimer: number | undefined;
    let valuationTimer: number | undefined;
    let reconnectDelayMs = 1000;
    let lastStatus = 'connecting';

    const scheduleReconnect = (delayMs: number) => {
      if (cancelled || reconnectTimer !== undefined) return;
      reconnectTimer = window.setTimeout(() => {
        reconnectTimer = undefined;
        void connect();
      }, delayMs);
    };

    const connect = async () => {
      try {
        const watchlists = await apiRequest<{ id: string; is_default: boolean }[]>('/market-data/watchlists');
        if (cancelled) return;
        const watchlist = watchlists.find((item) => item.is_default) ?? watchlists[0];
        if (!watchlist) {
          setDhanFeedStatus('unavailable');
          return;
        }
        const url = new URL(`/api/v1/market-data/watchlists/${watchlist.id}/stream`, window.location.href);
        url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
        socket = new WebSocket(url);
        socket.onmessage = (event) => {
          let message: {
            type?: string;
            status?: string;
            instrument_id?: string;
            security_id?: string | null;
            quote?: {
              close?: string;
              source?: string;
              market_timestamp?: string;
              market_age_seconds?: number;
              freshness_status?: string;
              is_synthetic?: boolean;
              is_verified?: boolean;
            };
          };
          try {
            message = JSON.parse(String(event.data)) as { type?: string; status?: string };
          } catch {
            setDhanFeedStatus('unavailable');
            return;
          }
          if (message.type === 'status' && message.status) {
            lastStatus = message.status;
            setDhanFeedStatus(message.status);
            if (message.status === 'connected') reconnectDelayMs = 1000;
            return;
          }
          if (message.type === 'tick') {
            const quote = message.quote;
            if (
              typeof message.instrument_id === 'string'
              && typeof quote?.close === 'string'
              && typeof quote.market_timestamp === 'string'
              && typeof quote.source === 'string'
            ) {
              const liveQuote: LiveWatchlistQuote = {
                instrument_id: message.instrument_id,
                security_id: message.security_id ?? null,
                close: quote.close,
                source: quote.source,
                market_timestamp: quote.market_timestamp,
                market_age_seconds: quote.market_age_seconds ?? 0,
                freshness_status: quote.freshness_status ?? 'fresh',
                is_synthetic: quote.is_synthetic ?? false,
                is_verified: quote.is_verified ?? true,
              };
              setLiveWatchlistQuotes((current) => ({
                ...current,
                [liveQuote.instrument_id]: liveQuote,
              }));
            }
            lastStatus = 'connected';
            setDhanFeedStatus('connected');
            if (valuationTimer !== undefined) window.clearTimeout(valuationTimer);
            valuationTimer = window.setTimeout(() => {
              valuationTimer = undefined;
              void refreshDhanPortfolio().catch((error: unknown) => {
                setDhanError(error instanceof Error ? error.message : 'Live portfolio valuation is unavailable.');
              });
            }, 100);
          }
        };
        socket.onerror = () => setDhanFeedStatus('unavailable');
        socket.onclose = () => {
          socket = null;
          if (cancelled) return;
          if (lastStatus === 'market_closed') {
            setDhanFeedStatus('market_closed');
            scheduleReconnect(nextNseOpenDelayMs());
          } else {
            setDhanFeedStatus('reconnecting');
            scheduleReconnect(reconnectDelayMs);
            reconnectDelayMs = Math.min(60_000, reconnectDelayMs * 2);
          }
        };
      } catch (error) {
        if (!cancelled) {
          setDhanFeedStatus('unavailable');
          setDhanError(error instanceof Error ? error.message : 'Live Dhan feed is unavailable.');
          scheduleReconnect(reconnectDelayMs);
          reconnectDelayMs = Math.min(60_000, reconnectDelayMs * 2);
        }
      }
    };

    void connect();
    return () => {
      cancelled = true;
      if (reconnectTimer !== undefined) window.clearTimeout(reconnectTimer);
      if (valuationTimer !== undefined) window.clearTimeout(valuationTimer);
      socket?.close();
    };
  }, [refreshDhanData, refreshDhanPortfolio]);

  useEffect(() => {
    if (dhanFeedStatus !== 'connected' || !dhanPortfolio) return;
    const intervalMs = Math.max(30, dhanPortfolio.snapshot_min_interval_seconds) * 1000;
    const timer = window.setInterval(() => {
      if (document.visibilityState !== 'visible') return;
      void apiRequest<{ sync_status?: string; reason?: string }>(
        '/brokers/dhan/sync?snapshot_only=true',
        { method: 'POST' },
      ).then(async (result) => {
        if (result.reason === 'market_closed') {
          setDhanFeedStatus('market_closed');
          return;
        }
        await refreshDhanPortfolio();
      }).catch((error: unknown) => {
        setDhanError(error instanceof Error ? error.message : 'Automatic Dhan snapshot refresh failed.');
      });
    }, intervalMs);
    return () => window.clearInterval(timer);
  }, [dhanFeedStatus, dhanPortfolio?.snapshot_min_interval_seconds, refreshDhanPortfolio]);

  const syncDhanPortfolio = async () => {
    setDhanSyncing(true);
    setDhanError(null);
    try {
      await apiRequest('/brokers/dhan/sync', { method: 'POST' });
      await refreshDhanData();
    } catch (error) {
      setDhanError(error instanceof Error ? error.message : 'Dhan account sync failed.');
    } finally {
      setDhanSyncing(false);
    }
  };
  const instrumentIdForSymbol = (symbol: string) =>
    valuationBySymbol.get(symbol)?.instrument_id
    ?? coverage?.managed.find((entry) => entry.symbol === symbol)?.instrument_id
    ?? coverage?.unmanaged.find((entry) => entry.symbol === symbol)?.instrument_id
    ?? null;
  const openMarketInstrument = (instrumentId: string) => {
    setOpenMarketInstrumentId(instrumentId);
    setActiveView('market-data');
  };

  return (
    <div className="overview-page">
      <section className="data-section" aria-label="Broker-sourced portfolio totals">
        <div className="section-heading">
          <div><p className="eyebrow">BROKER MIRROR · INR · READ ONLY</p><h2>Portfolio totals</h2></div>
          <span className="broker-sync-time">
            {dhanLoading ? 'Loading broker data…' : brokerSummary?.sources.length
              ? `Sources: ${brokerSummary.sources.join(', ')}`
              : 'No synced broker totals'}
          </span>
        </div>
        {dhanError && <p className="broker-sync-time" role="alert">{dhanError}</p>}
        <section className="metrics-row portfolio-metrics" aria-label="Broker portfolio totals">
          {([
            ['Cash balance', 'cash'],
            ['Invested · holdings', 'invested_cost'],
            ['Intraday exposure', 'positions_exposure'],
            ['Market value', 'market_value'],
            ['Unrealized P&L', 'unrealized_pnl'],
            ['Day change', 'day_change'],
          ] as const).map(([label, field]) => {
            const value = brokerSummary?.totals[field] ?? null;
            const basis = field === 'positions_exposure'
              && brokerSummary?.brokers.every((broker) => broker.positions_count === 0)
              ? 'No open positions'
              : brokerSummary?.brokers
              .map((broker) => compactBasis(broker, field))
              .filter(Boolean)
              .join(' + ');
            const unavailableReason = brokerSummary?.brokers
              .map((broker) => broker.unavailable_reasons[field])
              .filter((reason): reason is string => Boolean(reason))
              .join(' · ')
              || `No broker snapshot is available for ${label.toLowerCase()}.`;
            const changeValue = field === 'unrealized_pnl' || field === 'day_change';
            const changeClass = value !== null && changeValue
              ? Number(value) >= 0 ? 'positive' : 'negative'
              : undefined;
            return <article className="metric" key={field}>
              <span className="portfolio-metric-label">{label}</span>
              <strong className={changeClass}>
                {value === null
                  ? dhanLoading
                    ? 'Loading…'
                    : <UnavailableValue reason={unavailableReason} />
                  : formatMoney(value, 'INR')}
              </strong>
              {value !== null && basis && <small className="portfolio-metric-basis">{basis}</small>}
            </article>;
          })}
        </section>
        {!!brokerSummary?.brokers.length && <PortfolioEvidence brokers={brokerSummary.brokers} />}
        {!!brokerSummary?.brokers.length && <div className="table-scroll">
          <details className="portfolio-breakdown">
            <summary>Broker breakdown</summary>
            <table className="portfolio-summary-table">
            <thead><tr><th>Broker</th><th>Cash (INR)</th><th>Invested cost — delivery holdings (INR)</th><th>Positions exposure — cost basis (INR)</th><th>Holdings unrealized P&amp;L (INR)</th><th>Positions unrealized P&amp;L (INR)</th><th>Market value — holdings + positions (INR)</th><th>Total value — cash + holdings + positions (INR)</th><th>Unrealized P&amp;L — holdings + positions (INR)</th><th>Day change — holdings + positions (INR)</th><th>Synced</th></tr></thead>
            <tbody>
              {brokerSummary.brokers.map((broker) => <tr key={broker.source}>
                <td className="instrument-cell">{broker.source}</td>
                {(['cash', 'invested_cost', 'positions_exposure', 'holdings_unrealized_pnl', 'positions_unrealized_pnl', 'market_value', 'total_value', 'unrealized_pnl', 'day_change'] as const).map((field) => (
                  <td key={field}>
                    {broker[field] === null
                      ? <UnavailableValue reason={broker.unavailable_reasons[field] ?? `${portfolioFigureLabel(field)} is unavailable for ${broker.source}.`} />
                      : formatMoney(broker[field]!, broker.currency)}
                  </td>
                ))}
                <td>{broker.synced_at ? new Date(broker.synced_at).toLocaleString() : <UnavailableValue reason={`No broker sync timestamp is available for ${broker.source}.`} />}</td>
              </tr>)}
              {brokerSummary.brokers.length > 1 && <tr>
                <th>Combined</th>
                {(['cash', 'invested_cost', 'positions_exposure', 'holdings_unrealized_pnl', 'positions_unrealized_pnl', 'market_value', 'total_value', 'unrealized_pnl', 'day_change'] as const).map((field) => {
                  const value = brokerSummary.totals[field];
                  const reason = brokerSummary.brokers
                    .map((broker) => broker.unavailable_reasons[field])
                    .filter((item): item is string => Boolean(item))
                    .join(' · ')
                    || `Combined ${portfolioFigureLabel(field).toLowerCase()} is unavailable.`;
                  return <td key={field}>{value === null ? <UnavailableValue reason={reason} /> : formatMoney(value, 'INR')}</td>;
                })}
                <td>{brokerSummary.totals.synced_at ? new Date(brokerSummary.totals.synced_at).toLocaleString() : <UnavailableValue reason="No combined broker sync timestamp is available." />}</td>
              </tr>}
            </tbody>
            </table>
          </details>
        </div>}
      </section>
      {!loadingPortfolio && summary && <>
        <section className="data-section" aria-label="Dhan read-only portfolio">
          <div className="section-heading">
            <div><p className="eyebrow">READ-ONLY BROKER DATA · INR</p><h2>Dhan account</h2></div>
            <button className="secondary-button" type="button" disabled={dhanSyncing} onClick={() => void syncDhanPortfolio()}>
              {dhanSyncing ? 'Syncing…' : 'Sync Dhan'}
            </button>
          </div>
          <p className="broker-sync-time">
            Live feed: {dhanFeedStatus === 'connected' ? 'Connected' : dhanFeedStatus === 'market_closed' ? 'Idle · market closed' : dhanFeedStatus}
          </p>
          {dhanLoading ? <div className="table-empty">Loading stored Dhan account data…</div> : <>
            {dhanPortfolio?.synced_at && <p className="broker-sync-time">Source: Dhan · Last synced {new Date(dhanPortfolio.synced_at).toLocaleString()}</p>}
            {dhanPortfolio && <p className="broker-sync-time">
              {dhanPortfolio.instrument_master.last_refreshed_at
                ? `Dhan instrument master last refreshed ${new Date(dhanPortfolio.instrument_master.last_refreshed_at).toLocaleString()}.`
                : 'Dhan instrument master has not been loaded in this process.'}
            </p>}
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
                      {candidate.listing_date && ` · Listed ${candidate.listing_date}`}
                      {` · ${candidate.status}`}
                      {candidate.reason && ` · ${candidate.reason}`}
                      {candidate.verification_required && ` · ${candidate.verification_required}`}
                    </li>
                  ))}
                </ul>
              </details>
            )}
            <div className="content-grid broker-data-grid">
              <section className="data-section">
                <div className="section-heading"><div><h3>Holdings</h3></div><span className="section-count">{dhanPortfolio?.holdings.length ?? 0} records</span></div>
                {!dhanPortfolio?.holdings.length ? <div className="table-empty">No Dhan holdings have been synced.</div> : <div className="table-scroll"><table><thead><tr><th>Instrument</th><th>Exchange</th><th>Product</th><th>Quantity</th><th>Average cost (broker snapshot)</th><th>Live LTP</th><th>Live market value</th><th>Live unrealized P&amp;L</th><th>ISIN</th></tr></thead><tbody>{dhanPortfolio.holdings.map((holding) => {
                  const quote = holding.instrument_id ? liveWatchlistQuotes[holding.instrument_id] : undefined;
                  return <tr key={`${holding.security_id}-${holding.exchange}`}><td className="instrument-cell">{holding.symbol}<small className="trade-subtext">{holding.security_id ?? ''}</small></td><td>{holding.exchange}{holding.exchange_resolution_reason && <small className="trade-subtext">{holding.exchange_raw} · {holding.exchange_resolution_reason}</small>}</td><td>{holding.product || '—'}</td><td>{formatQuantity(holding.quantity)}<small className="trade-subtext">Broker snapshot · {new Date(holding.synced_at).toLocaleString()}</small></td><td>{holding.average_price ? formatMoney(holding.average_price, 'INR') : '—'}<small className="trade-subtext">Broker snapshot · {new Date(holding.synced_at).toLocaleString()}</small></td><td>{quote ? formatMoney(quote.close, 'INR') : '—'}<small className="trade-subtext">{quote ? `Dhan live feed · ${new Date(quote.market_timestamp).toLocaleString()}` : 'No live packet received'}</small></td><td>{holding.live_valuation.market_value !== null ? formatMoney(holding.live_valuation.market_value, 'INR') : '—'}<small className="trade-subtext">{holding.live_valuation.status === 'live' && holding.live_valuation.quote_at ? `Derived from live LTP · ${new Date(holding.live_valuation.quote_at).toLocaleString()}` : 'Stale · no fresh live quote'}</small></td><td>{holding.live_valuation.unrealized_profit !== null ? formatMoney(holding.live_valuation.unrealized_profit, 'INR') : '—'}<small className="trade-subtext">{holding.live_valuation.status === 'live' && holding.live_valuation.quote_at ? `Derived from live LTP · ${new Date(holding.live_valuation.quote_at).toLocaleString()}` : 'Stale · broker snapshot retained'}</small></td><td>{holding.isin ?? '—'}</td></tr>;
                })}</tbody></table></div>}
              </section>
              <section className="data-section">
                <div className="section-heading"><div><h3>Open positions</h3><small className="trade-subtext">Broker snapshot and live feed valuation are shown separately.</small></div><span className="section-count">{dhanPortfolio?.positions.length ?? 0} records</span></div>
                {!dhanPortfolio?.positions.length ? <div className="table-empty">No Dhan open positions have been synced.</div> : <div className="table-scroll"><table><thead><tr><th>Instrument</th><th>Exchange</th><th>Product / type</th><th>Net quantity (broker snapshot)</th><th>Cost price (broker snapshot)</th><th>Realized P&amp;L (Dhan snapshot)</th><th>Unrealized P&amp;L (Dhan snapshot)</th><th>Live LTP</th><th>Live market value</th><th>Live unrealized P&amp;L</th></tr></thead><tbody>{dhanPortfolio.positions.map((position) => {
                  const quote = position.instrument_id ? liveWatchlistQuotes[position.instrument_id] : undefined;
                  return <tr key={`${position.security_id}-${position.exchange}-${position.product}`}><td className="instrument-cell">{position.symbol}<small className="trade-subtext">{position.security_id ?? ''}</small></td><td>{position.exchange}</td><td>{position.product || '—'}{position.position_type ? ` · ${position.position_type}` : ''}</td><td>{formatQuantity(position.quantity)}<small className="trade-subtext">Broker snapshot · {new Date(position.broker_snapshot_at).toLocaleString()}</small></td><td>{position.average_price ? formatMoney(position.average_price, 'INR') : '—'}<small className="trade-subtext">Broker snapshot · {new Date(position.broker_snapshot_at).toLocaleString()}</small></td><td>{position.realized_profit === null ? '—' : formatMoney(position.realized_profit, 'INR')}<small className="trade-subtext">Broker snapshot · {new Date(position.broker_snapshot_at).toLocaleString()}</small></td><td>{position.unrealized_profit === null ? '—' : formatMoney(position.unrealized_profit, 'INR')}<small className="trade-subtext">Broker snapshot · {new Date(position.broker_snapshot_at).toLocaleString()}</small></td><td>{quote ? formatMoney(quote.close, 'INR') : '—'}<small className="trade-subtext">{quote ? `Dhan live feed · ${new Date(quote.market_timestamp).toLocaleString()}` : 'No live packet received'}</small></td><td>{position.live_valuation.market_value !== null ? formatMoney(position.live_valuation.market_value, 'INR') : '—'}<small className="trade-subtext">{position.live_valuation.status === 'live' && position.live_valuation.quote_at ? `Derived from live LTP · ${new Date(position.live_valuation.quote_at).toLocaleString()}` : 'Stale · broker snapshot retained'}</small></td><td>{position.live_valuation.unrealized_profit !== null ? formatMoney(position.live_valuation.unrealized_profit, 'INR') : '—'}<small className="trade-subtext">{position.live_valuation.status === 'live' && position.live_valuation.quote_at ? `Derived from live LTP · ${new Date(position.live_valuation.quote_at).toLocaleString()}` : 'Stale · broker snapshot retained'}</small></td></tr>;
                })}</tbody></table></div>}
                {!!dhanPortfolio?.closed_positions.length && <details className="closed-positions-details">
                  <summary>Closed today · {dhanPortfolio.closed_positions.length} records · realized P&amp;L retained</summary>
                  <div className="table-scroll"><table><thead><tr><th>Instrument</th><th>Exchange</th><th>Product / type</th><th>Net quantity</th><th>Realized P&amp;L (Dhan)</th></tr></thead><tbody>{dhanPortfolio.closed_positions.map((position, index) => <tr key={`${position.security_id}-${position.exchange}-${position.product}-${index}`}><td className="instrument-cell">{position.symbol}<small className="trade-subtext">{position.security_id ?? ''}</small></td><td>{position.exchange}</td><td>{position.product || '—'}{position.position_type ? ` · ${position.position_type}` : ''}</td><td>{formatQuantity(position.quantity)}</td><td>{position.realized_profit === null ? '—' : formatMoney(position.realized_profit, 'INR')}</td></tr>)}</tbody></table></div>
                </details>}
                {!!dhanPortfolio?.delivery_sales.length && <details className="closed-positions-details">
                  <summary>CNC sales from delivery holdings · {dhanPortfolio.delivery_sales.length} records</summary>
                  <div className="table-scroll"><table><thead><tr><th>Instrument</th><th>Broker exchange</th><th>Net quantity sold</th><th>Average price</th></tr></thead><tbody>{dhanPortfolio.delivery_sales.map((sale, index) => <tr key={`${sale.security_id}-${sale.exchange}-${index}`}><td className="instrument-cell">{sale.symbol}<small className="trade-subtext">{sale.security_id ?? ''}</small></td><td>{sale.exchange}{sale.exchange_resolution_reason && <small className="trade-subtext">{sale.exchange_raw} · {sale.exchange_resolution_reason}</small>}</td><td>{formatQuantity(sale.quantity)}</td><td>{sale.average_price ? formatMoney(sale.average_price, 'INR') : '—'}</td></tr>)}</tbody></table></div>
                </details>}
              </section>
            </div>
          </>}
        </section>
        <section className="data-section watchlist-section">
          <div className="section-heading">
            <div><p className="eyebrow">MARKET MONITORING</p><h2>Watchlist</h2></div>
            <button className="text-button" type="button" onClick={() => setActiveView('market-data')}>Open markets</button>
          </div>
          {watchlist.length === 0 ? <div className="table-empty">No instruments on your watchlist. Add one from Markets to see its live quote and data quality here.</div> : (
            <div className="table-scroll"><table><thead><tr><th>Instrument</th><th>Market</th><th>Latest quote</th><th>Quality</th><th>Observed</th><th /></tr></thead><tbody>
              {watchlist.map((item) => {
                const quote = liveWatchlistQuotes[item.instrument_id] ?? item.quote;
                const observedAt = quote ? new Date(quote.market_timestamp).toLocaleString() : '';
                const quality = item.relink_required
                  ? 'relink required'
                  : item.is_synthetic || quote?.is_synthetic
                    ? 'unavailable'
                    : quote?.freshness_status.replaceAll('_', ' ') ?? 'unavailable';
                const source = quote?.source ?? 'No quote received';
                return <tr
                  key={item.instrument_id}
                  className="overview-watchlist-row"
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
                  <td className="instrument-cell">{item.symbol}<small className="trade-subtext">{item.display_name}</small></td>
                  <td>{item.venue} · {item.asset_class}</td>
                  <td>{quote ? formatMoney(quote.close, item.currency) : '—'}</td>
                  <td>
                    {item.relink_required || item.is_synthetic || quote?.is_synthetic || !quote
                      ? <span className="freshness-badge freshness-unavailable" title={`${quality} · ${source}`}>{quality}</span>
                      : <span className={`freshness-badge freshness-${quote.freshness_status}`} title={`${quality} · Source: ${source}`}>{quality}</span>}
                  </td>
                  <td title={quote ? `Observed ${observedAt}` : undefined}>{quote ? relativeAge(quote.market_age_seconds) : '—'}</td>
                  <td><button className="text-button watchlist-row-action" type="button" onClick={(event) => { event.stopPropagation(); void removeInstrumentFromWatchlist(item.instrument_id); }}>Remove</button></td>
                </tr>;
              })}
            </tbody></table></div>
          )}
          <div className="valuation-footnote">Live prices use the Dhan watchlist stream and retain the packet timestamp. If no live packet is available, the stored quote remains visibly identified by its age and source.</div>
        </section>
        <div className="content-grid overview-workspace-grid">
          <section className="data-section">
            <div className="section-heading"><div><p className="eyebrow">MANUAL LEDGER · SEPARATE FROM BROKER TOTALS</p><h2>Ledger positions</h2></div><span className="section-count">{summary.positions.length} records</span></div>
            {summary.positions.length === 0 ? <div className="table-empty">No open positions recorded.</div> : (
              <div className="table-scroll"><table><thead><tr><th>Instrument</th><th>Quantity</th><th>Held since</th><th>Average cost</th><th>Cost basis</th><th>Last price</th><th>Market value</th><th>Unrealized P&amp;L</th><th>Day change</th><th>Weight</th><th>Data quality</th><th>Thesis</th><th>Watchlist</th></tr></thead><tbody>{summary.positions.map((position) => {
                const priced = valuationBySymbol.get(position.symbol);
                const instrumentId = instrumentIdForSymbol(position.symbol);
                const liveQuote = instrumentId ? liveWatchlistQuotes[instrumentId] : undefined;
                const needsAttention = !priced || priced.freshness === 'stale' || priced.is_synthetic || priced.freshness === 'unavailable';
                const isWatchlisted = Boolean(priced?.instrument_id && watchlist.some((item) => item.instrument_id === priced.instrument_id));
                return <tr key={position.symbol}>
                  <td className="instrument-cell">{position.symbol}</td>
                  <td>{formatQuantity(position.quantity)}</td>
                  <td>{position.holding_period_start ? new Date(position.holding_period_start).toLocaleDateString() : '—'}</td>
                  <td>{formatMoney(position.average_cost, currency)}</td>
                  <td>{formatMoney(position.cost_basis, currency)}</td>
                  <td title={liveQuote ? `Dhan live feed · ${new Date(liveQuote.market_timestamp).toLocaleString()}` : undefined}>{liveQuote ? formatMoney(liveQuote.close, currency) : priced?.last_price ? formatMoney(priced.last_price, currency) : '—'}</td>
                  <td>{priced?.market_value ? formatMoney(priced.market_value, currency) : '—'}</td>
                  <td className={priced?.unrealized_pnl === null || priced?.unrealized_pnl === undefined ? '' : Number(priced.unrealized_pnl) >= 0 ? 'positive' : 'negative'}>{priced?.unrealized_pnl ? formatMoney(priced.unrealized_pnl, currency) : '—'}</td>
                  <td>{priced?.day_change ? formatMoney(priced.day_change, currency) : '—'}{priced?.day_change_percent ? ` (${Number(priced.day_change_percent).toFixed(2)}%)` : ''}</td>
                  <td>{priced?.weight_percent ? `${Number(priced.weight_percent).toFixed(2)}%` : '—'}</td>
                  <td>{needsAttention ? <span className={`freshness-badge freshness-${priced?.is_synthetic ? 'unavailable' : priced?.freshness ?? 'unavailable'}`}>{priced?.is_synthetic ? 'unavailable' : priced?.freshness ?? 'unlinked'}</span> : <span className="freshness-badge freshness-fresh">verified · recent</span>}</td>
                  <td>{(() => {
                    const managed = coverage?.managed.find((entry) => entry.symbol === position.symbol);
                    return <button className="secondary-button thesis-button" type="button" onClick={() => setThesisSymbol(position.symbol)}>{coverage === null ? 'Thesis' : managed ? (managed.review_overdue ? 'Review due' : 'Edit thesis') : 'Unmanaged · add thesis'}</button>;
                  })()}</td>
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
            <div className="valuation-footnote"><span className={`status-dot ${marketDataStatus}`} /> {valuation?.confidence === 'unreliable' ? `Valuation is unreliable: ${valuation.reasons.join(' ')}` : `Open cost basis ${formatMoney(summary.invested_cost, currency)} · market prices are not FX-converted.`}</div>
          </section>
          <TransactionEntry {...workspace} />
        </div>
      </>}
    </div>
  );
}
