import { useCallback, useEffect, useMemo, useState } from 'react';
import { apiRequest } from '../api';

type FeedStatus = {
  state: string;
  connected: boolean;
  reconnects: number;
  consecutive_failures: number;
  dropped_events: number;
  last_event_at: string | null;
  last_error: string | null;
};

type DeltaResearchStatus = {
  enabled: boolean;
  milestone: number;
  mode: string;
  store_status: 'disabled' | 'not_configured' | 'not_found' | 'invalid' | 'present';
  store_modified_at: string | null;
  store_age_seconds: number | null;
  live_feeds: string;
  account_monitor: 'disabled';
  warnings: string[];
  disclaimer: string;
  feed: FeedStatus;
  collection_interval_seconds: number;
};

type Overview = {
  summary: {
    products: number;
    quotes: number;
    candles: number;
    market_events: number;
    latest_quote_at: string | null;
    latest_candle_at: string | null;
  };
};

type Market = {
  product_id: string;
  symbol: string;
  contract_type: string;
  state: string;
  quote_time: string | null;
  last_price: string | null;
  mark_price: string | null;
  index_price: string | null;
  volume_24h: string | null;
  quote_source: string | null;
};

type EventRow = {
  id: number;
  symbol: string | null;
  event_type: string;
  received_at: string;
  exchange_time: string | null;
};

type ProductDetail = {
  product: { symbol: string; contract_type: string; state: string };
  quote: {
    observed_at: string;
    source: string;
    last_price: string | null;
    mark_price: string | null;
    index_price: string | null;
  } | null;
  candles: Array<{
    timeframe: string;
    observed_at: string;
    open: string;
    high: string;
    low: string;
    close: string;
    volume: string | null;
    source: string;
  }>;
};

type Tab = 'overview' | 'markets' | 'events' | 'strategies' | 'evaluations';

const tabs: Array<{ id: Tab; label: string }> = [
  { id: 'overview', label: 'Overview' },
  { id: 'markets', label: 'Markets' },
  { id: 'events', label: 'Events' },
  { id: 'strategies', label: 'Strategy evidence' },
  { id: 'evaluations', label: 'Evaluations & leaderboard' },
];

const dateLabel = (value: string | null | undefined) =>
  value ? new Date(value).toLocaleString() : 'Not available';

export function DeltaResearchPage() {
  const [status, setStatus] = useState<DeltaResearchStatus | null>(null);
  const [overview, setOverview] = useState<Overview | null>(null);
  const [markets, setMarkets] = useState<Market[]>([]);
  const [events, setEvents] = useState<EventRow[]>([]);
  const [detail, setDetail] = useState<ProductDetail | null>(null);
  const [selectedProduct, setSelectedProduct] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState<Tab>('overview');
  const [search, setSearch] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [dataError, setDataError] = useState<string | null>(null);

  const refreshData = useCallback(async (query = '') => {
    const [overviewResult, marketsResult, eventsResult] = await Promise.allSettled([
      apiRequest<Overview>('/delta-research/overview'),
      apiRequest<{ items: Market[] }>(
        `/delta-research/markets?limit=100&search=${encodeURIComponent(query)}`,
      ),
      apiRequest<{ items: EventRow[] }>('/delta-research/events?limit=50'),
    ]);
    if (overviewResult.status === 'fulfilled') setOverview(overviewResult.value);
    if (marketsResult.status === 'fulfilled') setMarkets(marketsResult.value.items);
    if (eventsResult.status === 'fulfilled') setEvents(eventsResult.value.items);
    const failed = [overviewResult, marketsResult, eventsResult].find(
      (result) => result.status === 'rejected',
    );
    setDataError(
      failed?.status === 'rejected'
        ? failed.reason instanceof Error
          ? failed.reason.message
          : 'Public research data is unavailable.'
        : null,
    );
  }, []);

  useEffect(() => {
    let cancelled = false;
    let source: EventSource | null = null;
    apiRequest<DeltaResearchStatus>('/delta-research/status')
      .then(async (result) => {
        if (cancelled) return;
        setStatus(result);
        if (!result.enabled || result.store_status !== 'present') return;
        await refreshData();
        if (cancelled) return;
        source = new EventSource('/api/v1/delta-research/stream', { withCredentials: true });
        source.addEventListener('snapshot', (event) => {
          if (cancelled) return;
          try {
            const snapshot = JSON.parse((event as MessageEvent<string>).data) as FeedStatus;
            setStatus((current) => current
              ? { ...current, feed: snapshot, live_feeds: snapshot.state }
              : current);
          } catch {
            setDataError('The public feed sent an unreadable status snapshot.');
          }
        });
        source.addEventListener('health', (event) => {
          if (cancelled) return;
          try {
            const next = JSON.parse((event as MessageEvent<string>).data) as {
              feed?: FeedStatus;
            } & FeedStatus;
            setStatus((current) => current
              ? { ...current, feed: next.feed ?? next, live_feeds: (next.feed ?? next).state }
              : current);
          } catch {
            setDataError('The public feed sent an unreadable health update.');
          }
        });
        source.addEventListener('market_data', (event) => {
          if (cancelled) return;
          try {
            const update = JSON.parse((event as MessageEvent<string>).data) as {
              last_event_at?: string | null;
            };
            if (update.last_event_at) {
              setStatus((current) => current
                ? {
                    ...current,
                    feed: { ...current.feed, last_event_at: update.last_event_at ?? null },
                  }
                : current);
            }
          } catch {
            setDataError('The public feed sent an unreadable market-data update.');
          }
          void refreshData();
        });
      })
      .catch((requestError: unknown) => {
        if (!cancelled) {
          setError(
            requestError instanceof Error
              ? requestError.message
              : 'Delta research status is unavailable.',
          );
        }
      });
    return () => {
      cancelled = true;
      source?.close();
    };
  }, [refreshData]);

  useEffect(() => {
    if (!selectedProduct) {
      setDetail(null);
      return;
    }
    let cancelled = false;
    apiRequest<ProductDetail>(`/delta-research/markets/${encodeURIComponent(selectedProduct)}`)
      .then((result) => { if (!cancelled) setDetail(result); })
      .catch((requestError: unknown) => {
        if (!cancelled) {
          setDataError(
            requestError instanceof Error
              ? requestError.message
              : 'Product details are unavailable.',
          );
        }
      });
    return () => { cancelled = true; };
  }, [selectedProduct]);

  const feed = status?.feed;
  const marketRows = useMemo(() => markets, [markets]);

  return (
    <div className="overview-page">
      <section className="data-section" aria-label="Delta public market research">
        <div className="section-heading">
          <div>
            <p className="eyebrow">DELTA EXCHANGE · PUBLIC MARKET DATA · READ ONLY</p>
            <h2>Delta Research</h2>
          </div>
          {status && <span className="section-count">Milestone {status.milestone}</span>}
        </div>
        {error && <p className="broker-sync-time" role="alert">{error}</p>}
        {dataError && <p className="broker-sync-time" role="alert">{dataError}</p>}
        {!status && !error && <div className="table-empty" role="status">Loading Delta research…</div>}
        {status && <>
          <div className="metrics-row" aria-label="Public research health">
            <article className="metric"><span>Collection</span><strong>{status.enabled ? 'Enabled' : 'Disabled'}</strong></article>
            <article className="metric"><span>Feed</span><strong>{feed?.state ?? status.live_feeds}</strong></article>
            <article className="metric"><span>Products</span><strong>{overview?.summary.products ?? 'Not available'}</strong></article>
            <article className="metric"><span>Last packet</span><strong>{dateLabel(feed?.last_event_at)}</strong></article>
          </div>
          <p className="broker-sync-time">
            Public feed only · Snapshot interval {status.collection_interval_seconds}s · Account access disabled
          </p>
          {feed?.last_error && <p className="broker-sync-time" role="alert">{feed.last_error}</p>}
          {status.warnings.length > 0 && (
            <ul className="portfolio-provider-attempts">
              {status.warnings.map((warning) => <li key={warning}>{warning}</li>)}
            </ul>
          )}
          <nav className="section-tabs" aria-label="Delta research sections">
            {tabs.map((tab) => (
              <button
                type="button"
                key={tab.id}
                aria-pressed={activeTab === tab.id}
                onClick={() => setActiveTab(tab.id)}
              >
                {tab.label}
              </button>
            ))}
          </nav>

          {activeTab === 'overview' && (
            <section aria-label="Research data coverage">
              <h3>Stored public data</h3>
              <p>
                Quotes: {overview?.summary.quotes ?? 'Not available'} ·
                Candles: {overview?.summary.candles ?? 'Not available'} ·
                Events: {overview?.summary.market_events ?? 'Not available'}
              </p>
              <p className="broker-sync-time">
                Latest quote {dateLabel(overview?.summary.latest_quote_at)} ·
                Latest candle {dateLabel(overview?.summary.latest_candle_at)}
              </p>
              <p>Market prices and market events are observations, not investment recommendations.</p>
            </section>
          )}

          {activeTab === 'markets' && (
            <section aria-label="Public markets">
              <label>
                Search markets
                <input
                  type="search"
                  value={search}
                  onChange={(event) => {
                    setSearch(event.currentTarget.value);
                    void refreshData(event.currentTarget.value);
                  }}
                  maxLength={80}
                />
              </label>
              <table>
                <thead><tr><th>Instrument</th><th>State</th><th>Last price</th><th>Mark</th><th>Observed</th><th>Source</th></tr></thead>
                <tbody>
                  {marketRows.map((market) => (
                    <tr key={market.product_id}>
                      <td><button type="button" onClick={() => setSelectedProduct(market.product_id)}>{market.symbol}</button></td>
                      <td>{market.state}</td>
                      <td>{market.last_price ?? 'Not available'}</td>
                      <td>{market.mark_price ?? 'Not available'}</td>
                      <td>{dateLabel(market.quote_time)}</td>
                      <td>{market.quote_source ?? 'Not available'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {marketRows.length === 0 && <p className="table-empty">No public market rows are available.</p>}
              {detail && (
                <section aria-label={`${detail.product.symbol} details`}>
                  <h3>{detail.product.symbol} · {detail.product.contract_type}</h3>
                  <p>State: {detail.product.state}</p>
                  <p>
                    Latest quote: {detail.quote?.last_price ?? 'Not available'} ·
                    Source: {detail.quote?.source ?? 'Not available'} ·
                    Observed: {dateLabel(detail.quote?.observed_at)}
                  </p>
                  <h4>Recent 1-minute candles</h4>
                  <table>
                    <thead><tr><th>Time</th><th>Open</th><th>High</th><th>Low</th><th>Close</th><th>Source</th></tr></thead>
                    <tbody>{detail.candles.slice(-20).map((candle) => (
                      <tr key={`${candle.timeframe}-${candle.observed_at}`}>
                        <td>{dateLabel(candle.observed_at)}</td><td>{candle.open}</td><td>{candle.high}</td>
                        <td>{candle.low}</td><td>{candle.close}</td><td>{candle.source}</td>
                      </tr>
                    ))}</tbody>
                  </table>
                </section>
              )}
            </section>
          )}

          {activeTab === 'events' && (
            <section aria-label="Recent public market events">
              <table>
                <thead><tr><th>Type</th><th>Instrument</th><th>Received</th><th>Exchange time</th></tr></thead>
                <tbody>{events.map((item) => (
                  <tr key={item.id}>
                    <td>{item.event_type}</td><td>{item.symbol ?? 'Market-wide'}</td>
                    <td>{dateLabel(item.received_at)}</td><td>{dateLabel(item.exchange_time)}</td>
                  </tr>
                ))}</tbody>
              </table>
              {events.length === 0 && <p className="table-empty">No public market events have been stored.</p>}
            </section>
          )}

          {(activeTab === 'strategies' || activeTab === 'evaluations') && (
            <section aria-label={activeTab === 'strategies' ? 'Strategy evidence' : 'Evaluation evidence'}>
              <h3>{activeTab === 'strategies' ? 'Strategy evidence' : 'Evaluation evidence'}</h3>
              <p>
                {activeTab === 'strategies'
                  ? 'Strategy evidence is not collected by this public-feed milestone.'
                  : 'No validated evaluation evidence is stored.'}
              </p>
              {activeTab === 'evaluations' && (
                <p>Leaderboard unavailable: validated evaluation evidence is not stored.</p>
              )}
              <p>Unavailable data is not inferred from market observations.</p>
            </section>
          )}
          <p className="broker-sync-time">{status.disclaimer}</p>
        </>}
      </section>
    </div>
  );
}
