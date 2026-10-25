import {
  ArrowUpRight, Clock3, Pencil, Plus, Search, ShieldCheck, Trash2, Wallet,
} from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { apiRequest } from '../api';
import { CandleChart } from '../components/CandleChart';
import { ThesisEditor } from '../components/ThesisEditor';
import {
  DataTable,
  EmptyState,
  ErrorState,
  Panel,
  Skeleton,
  StatusDot,
  type DataTableColumn,
} from '../components/ui';
import {
  formatMoney, formatQuantity, localDateTime, marketRangeParams,
  type WorkspaceState,
} from '../components/useWorkspace';

type FundamentalPeriod = {
  id: string;
  period_type: 'annual' | 'quarterly';
  period_end: string;
  currency: string;
  revenue: string | null;
  net_income: string | null;
  eps: string | null;
  operating_margin: string | null;
  total_assets: string | null;
  total_liabilities: string | null;
  total_equity: string | null;
  operating_cash_flow: string | null;
  free_cash_flow: string | null;
  total_debt: string | null;
  return_on_equity_pct: string | null;
  debt_to_equity: string | null;
  shares_outstanding: string | null;
  source: string;
  fetched_at: string;
  stale: boolean;
  revenue_yoy_growth_pct: string | null;
  net_income_yoy_growth_pct: string | null;
  eps_yoy_growth_pct: string | null;
  free_cash_flow_yoy_growth_pct: string | null;
};
type FundamentalsData = {
  instrument_id: string;
  symbol: string;
  currency: string;
  snapshot: {
    market_cap: string | null;
    pe: string | null;
    pb: string | null;
    dividend_yield: string | null;
    sector: string | null;
    industry: string | null;
    source: string;
    fetched_at: string;
    stale: boolean;
  } | null;
  fundamentals: FundamentalPeriod[];
  unavailable: boolean;
  message: string;
};
type FundamentalsSync = { instrument_id: string; status: 'available' | 'unavailable'; message: string };
type CoverageInstrument = {
  instrument_id: string;
  symbol: string;
  display_name: string;
  venue: string;
  bars_stored: number;
  oldest_bar: string | null;
  newest_bar: string | null;
  last_sync_status: string | null;
  last_sync_provider: string | null;
  last_sync_reason: string | null;
  freshness_status: 'fresh' | 'stale' | 'failed' | 'never-synced';
};
type CoverageResponse = { instruments: CoverageInstrument[] };
type BackfillResponse = {
  status: 'success' | 'partial' | 'unchanged';
  instruments?: { status: string; symbol: string; chunks_failed: number }[];
  symbol?: string;
  chunks_failed?: number;
};
type MarketDataSource = {
  name: string;
  label: string;
  available: boolean;
  status: string;
  enabled: boolean;
};
type MarketDataSettings = {
  selected_provider: string | null;
  sources: MarketDataSource[];
};
type MarketStreamStatus = 'not_configured' | 'connecting' | 'active' | 'reconnecting' | 'unavailable';
type MarketStreamMessage =
  | { type: 'status'; status: MarketStreamStatus }
  | { type: 'tick'; quote: NonNullable<WorkspaceState['marketQuote']> };

export function MarketDataPage({ workspace }: { workspace: Pick<WorkspaceState, 'addInstrumentToWatchlist' | 'addSelectedInstrumentToWatchlist' | 'attachSelectedInstrument' | 'currency' | 'hasOlderMarketHistory' | 'loadInstrumentIndicators' | 'loadMarketHistory' | 'loadMarketQuote' | 'loadOlderMarketHistory' | 'loadingMarketHistory' | 'loadingOlderMarketHistory' | 'loadingMarketQuote' | 'marketEnd' | 'marketHistory' | 'marketIndicators' | 'marketInstruments' | 'marketInterval' | 'marketQuery' | 'marketQuote' | 'marketStart' | 'removeInstrumentFromWatchlist' | 'searchMarketInstruments' | 'searchingMarket' | 'selectMarketInstrument' | 'selectedInstrument' | 'setMarketEnd' | 'setMarketHistory' | 'setMarketInterval' | 'setMarketQuery' | 'setMarketStart' | 'summary' | 'valuation' | 'watchlist'> }) {
  const {
  addInstrumentToWatchlist,
  addSelectedInstrumentToWatchlist,
  attachSelectedInstrument,
  currency,
  loadInstrumentIndicators,
  loadMarketHistory,
  loadOlderMarketHistory,
  loadingOlderMarketHistory,
  hasOlderMarketHistory,
  loadMarketQuote,
  loadingMarketHistory,
  loadingMarketQuote,
  marketEnd,
  marketHistory,
  marketIndicators,
  marketInstruments,
  marketInterval,
  marketQuery,
  marketQuote,
  marketStart,
  removeInstrumentFromWatchlist,
  searchMarketInstruments,
  searchingMarket,
  selectMarketInstrument,
  selectedInstrument,
  setMarketEnd,
  setMarketHistory,
  setMarketInterval,
  setMarketQuery,
  setMarketStart,
  summary,
  valuation,
  watchlist
} = workspace;
  const [fundamentals, setFundamentals] = useState<FundamentalsData | null>(null);
  const [fundamentalsLoading, setFundamentalsLoading] = useState(false);
  const [fundamentalsSyncing, setFundamentalsSyncing] = useState(false);
  const [fundamentalsMessage, setFundamentalsMessage] = useState('');
  const [fundamentalPeriodType, setFundamentalPeriodType] = useState<'annual' | 'quarterly'>('annual');
  const selectedInstrumentId = useRef<string | null>(null);
  selectedInstrumentId.current = selectedInstrument?.id ?? null;
  const [coverage, setCoverage] = useState<CoverageInstrument[]>([]);
  const [coverageLoading, setCoverageLoading] = useState(true);
  const [coverageError, setCoverageError] = useState('');
  const [coverageMessage, setCoverageMessage] = useState('');
  const [coverageRunning, setCoverageRunning] = useState<string | null>(null);
  const [coverageBatchRunning, setCoverageBatchRunning] = useState(false);
  const [liveFeedEnabled, setLiveFeedEnabled] = useState(false);
  const [liveFeedStatus, setLiveFeedStatus] = useState<MarketStreamStatus>('not_configured');
  const [liveFeedMessage, setLiveFeedMessage] = useState('');
  const [streamRetryKey, setStreamRetryKey] = useState(0);
  const [liveMarketQuote, setLiveMarketQuote] = useState<NonNullable<WorkspaceState['marketQuote']> | null>(null);
  const [marketDataSettings, setMarketDataSettings] = useState<MarketDataSettings | null>(null);
  const [marketDataSettingsLoading, setMarketDataSettingsLoading] = useState(true);
  const [marketDataSettingsError, setMarketDataSettingsError] = useState('');
  const displayedMarketQuote = liveMarketQuote ?? marketQuote;
  const selectedProvider = marketDataSettings?.selected_provider;
  const selectedSourceLabel = marketDataSettings?.sources.find(
    (source) => source.name === selectedProvider,
  )?.label ?? 'Market-data';
  const liveStreamSupported = ['dhan', 'angel_one'].includes(
    marketDataSettings?.selected_provider ?? '',
  );

  async function refreshMarketDataSettings() {
    setMarketDataSettingsError('');
    try {
      const settings = await apiRequest<MarketDataSettings>('/market-data/settings');
      setMarketDataSettings(settings);
      return settings;
    } catch (error) {
      setMarketDataSettingsError(error instanceof Error ? error.message : 'Market-data settings could not be loaded.');
      throw error;
    } finally {
      setMarketDataSettingsLoading(false);
    }
  }

  useEffect(() => {
    void refreshMarketDataSettings().catch(() => undefined);
  }, []);

  async function selectMarketDataSource(provider: string) {
    setMarketDataSettingsError('');
    try {
      const settings = await apiRequest<MarketDataSettings>(
        '/market-data/settings/provider',
        {
          method: 'PUT',
          body: JSON.stringify({ provider }),
        },
      );
      setMarketDataSettings(settings);
    } catch (error) {
      setMarketDataSettingsError(error instanceof Error ? error.message : 'Data source could not be selected.');
    }
  }

  useEffect(() => {
    if (!liveFeedEnabled || !selectedInstrument || !liveStreamSupported) {
      setLiveMarketQuote(null);
      setLiveFeedStatus('not_configured');
      if (liveFeedEnabled && !liveStreamSupported) setLiveFeedEnabled(false);
      return;
    }
    const streamUrl = new URL(
      `/api/v1/market-data/instruments/${selectedInstrument.id}/stream`,
      window.location.href,
    );
    streamUrl.protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    let intentionalClose = false;
    let retryTimer: number | undefined;
    setLiveMarketQuote(null);
    setLiveFeedStatus('connecting');
    setLiveFeedMessage('');
    const socket = new WebSocket(streamUrl);
    const heartbeat = window.setInterval(() => {
      if (socket.readyState === WebSocket.OPEN) socket.send('ping');
    }, 15_000);
    socket.onmessage = (event) => {
      let message: MarketStreamMessage;
      try {
        message = JSON.parse(event.data) as MarketStreamMessage;
      } catch {
        setLiveFeedStatus('unavailable');
        setLiveFeedMessage('The live feed returned an invalid message.');
        return;
      }
      if (message.type === 'status') {
        setLiveFeedStatus(message.status);
        if (message.status === 'unavailable') {
          setLiveFeedMessage(`${selectedSourceLabel} live feed is unavailable. Check the connection, permissions, and instrument mapping.`);
        }
        return;
      }
      setLiveMarketQuote(message.quote);
      setLiveFeedStatus('active');
      setLiveFeedMessage('');
    };
    socket.onerror = () => {
      setLiveFeedStatus('unavailable');
      setLiveFeedMessage(`The ${selectedSourceLabel} live feed connection failed.`);
    };
    socket.onclose = (event) => {
      if (!intentionalClose) {
        if (![4401, 4403, 4404].includes(event.code)) {
          setLiveFeedStatus('reconnecting');
          setLiveFeedMessage(`${selectedSourceLabel} live feed disconnected. Reconnecting…`);
          retryTimer = window.setTimeout(() => setStreamRetryKey((key) => key + 1), 2000);
        } else {
          setLiveFeedStatus('unavailable');
          setLiveFeedMessage(`${selectedSourceLabel} live feed is unavailable. Review authentication and instrument mapping.`);
        }
      }
    };
    return () => {
      intentionalClose = true;
      window.clearInterval(heartbeat);
      if (retryTimer !== undefined) window.clearTimeout(retryTimer);
      socket.close();
    };
  }, [liveFeedEnabled, liveStreamSupported, marketDataSettings?.selected_provider, selectedInstrument?.id, selectedSourceLabel, streamRetryKey]);

  async function loadCoverage() {
    setCoverageLoading(true);
    setCoverageError('');
    try {
      const response = await apiRequest<CoverageResponse>('/market-data/coverage');
      setCoverage(response.instruments);
    } catch (error) {
      setCoverageError(error instanceof Error ? error.message : 'Coverage could not be loaded.');
    } finally {
      setCoverageLoading(false);
    }
  }

  useEffect(() => {
    void loadCoverage();
  }, []);

  async function runBackfill(instrumentId?: string) {
    setCoverageMessage('');
    if (instrumentId) setCoverageRunning(instrumentId);
    else setCoverageBatchRunning(true);
    try {
      const path = instrumentId
        ? `/market-data/instruments/${instrumentId}/history/backfill?years=2`
        : '/market-data/backfill/holdings?years=2';
      const response = await apiRequest<BackfillResponse>(path, { method: 'POST' });
      const partial = response.status === 'partial'
        || response.chunks_failed
        || response.instruments?.some((item) => item.status === 'partial' || item.status === 'unavailable');
      setCoverageMessage(partial
        ? 'Backfill completed partially. Review each row’s status and last result.'
        : 'Backfill completed. Coverage has been refreshed.');
      await loadCoverage();
    } catch (error) {
      setCoverageMessage(error instanceof Error ? error.message : 'Backfill failed.');
    } finally {
      setCoverageRunning(null);
      setCoverageBatchRunning(false);
    }
  }

  function displayCoverageDate(value: string | null): string {
    return value ? new Date(value).toISOString().slice(0, 10) : '—';
  }

  function displaySyncReason(value: string | null): string {
    if (!value) return '—';
    const messages: Record<string, string> = {
      provider_data_already_stored: 'No new bars',
      provider_no_history_for_range: 'No bars returned for this range',
      provider_rate_limit_exceeded: 'Rate limited; retry later',
      provider_request_cap_reached: 'Request cap reached',
      provider_request_failed: 'Provider request failed',
      no_real_provider_available: 'No real provider configured',
      no_new_bars_written: 'No new bars',
    };
    const fallback = value.match(/^fallback_after_(angel_one|dhan|yfinance)_(.+)$/);
    if (fallback) {
      const providerLabels: Record<string, string> = {
        angel: 'Angel One',
        angel_one: 'Angel One',
        dhan: 'Dhan',
        yfinance: 'Yahoo Finance',
      };
      return `Fallback after ${providerLabels[fallback[1]] ?? fallback[1]} failed: ${fallback[2].replaceAll('_', ' ')}`;
    }
    if (value.includes(';')) {
      return value.split(';').map(displaySyncReason).join(' · ');
    }
    return messages[value] ?? value.replaceAll('_', ' ');
  }

  const coverageColumns: DataTableColumn<CoverageInstrument>[] = [
    {
      key: 'symbol',
      label: 'Instrument',
      render: (instrument) => (
        <span className="coverage-symbol" title={instrument.display_name}>
          {instrument.symbol} · {instrument.venue}
        </span>
      ),
      sortValue: (instrument) => instrument.symbol,
      className: 'coverage-symbol',
    },
    {
      key: 'bars',
      label: 'Real bars',
      render: (instrument) => instrument.bars_stored.toLocaleString(),
      sortValue: (instrument) => instrument.bars_stored,
      align: 'right',
    },
    {
      key: 'oldest',
      label: 'Oldest',
      render: (instrument) => displayCoverageDate(instrument.oldest_bar),
      sortValue: (instrument) => instrument.oldest_bar ?? '',
      align: 'right',
    },
    {
      key: 'newest',
      label: 'Newest',
      render: (instrument) => displayCoverageDate(instrument.newest_bar),
      sortValue: (instrument) => instrument.newest_bar ?? '',
      align: 'right',
    },
    {
      key: 'status',
      label: 'Status',
      render: (instrument) => {
        const status = instrument.freshness_status;
        const labels = {
          fresh: 'fresh',
          stale: 'stale',
          failed: 'failed',
          'never-synced': 'never-synced',
        };
        return <StatusDot status={status} label={labels[status]} />;
      },
      sortValue: (instrument) => instrument.freshness_status,
    },
    {
      key: 'reason',
      label: 'Last result',
      render: (instrument) => (
        <>
          {instrument.last_sync_provider && (
            <span>
              {instrument.last_sync_status === 'success' ? 'Served by ' : 'Attempted '}
              {instrument.last_sync_provider} ·{' '}
            </span>
          )}
          {displaySyncReason(instrument.last_sync_reason)}
        </>
      ),
      sortValue: (instrument) => instrument.last_sync_reason ?? '',
      className: 'coverage-reason',
    },
    {
      key: 'action',
      label: 'Action',
      render: (instrument) => (
        <button
          type="button"
          onClick={() => void runBackfill(instrument.instrument_id)}
          disabled={coverageBatchRunning || coverageRunning !== null}
          aria-label={`Backfill ${instrument.symbol} history`}
        >
          {coverageRunning === instrument.instrument_id ? 'Backfilling…' : 'Backfill'}
        </button>
      ),
      className: 'coverage-action',
    },
  ];

  useEffect(() => {
    if (!selectedInstrument) {
      setFundamentals(null);
      return;
    }
    let active = true;
    setFundamentals(null);
    setFundamentalsSyncing(false);
    setFundamentalsLoading(true);
    setFundamentalsMessage('');
    apiRequest<FundamentalsData>(`/market-data/instruments/${selectedInstrument.id}/fundamentals?period_type=${fundamentalPeriodType}`)
      .then((data) => {
        if (active) setFundamentals(data);
      })
      .catch((error: Error) => {
        if (active) setFundamentalsMessage(error.message);
      })
      .finally(() => {
        if (active) setFundamentalsLoading(false);
      });
    return () => {
      active = false;
    };
  }, [selectedInstrument?.id, fundamentalPeriodType]);

  async function syncFundamentals() {
    if (!selectedInstrument) return;
    setFundamentalsSyncing(true);
    setFundamentalsMessage('');
    try {
      const result = await apiRequest<FundamentalsSync>(
        `/market-data/instruments/${selectedInstrument.id}/fundamentals/sync`,
        { method: 'POST', body: '{}' },
      );
      if (selectedInstrumentId.current !== result.instrument_id) return;
      setFundamentalsMessage(result.message);
      const data = await apiRequest<FundamentalsData>(
        `/market-data/instruments/${result.instrument_id}/fundamentals?period_type=${fundamentalPeriodType}`,
      );
      if (selectedInstrumentId.current === data.instrument_id) setFundamentals(data);
    } catch (error) {
      if (selectedInstrumentId.current === selectedInstrument.id) {
        setFundamentalsMessage(error instanceof Error ? error.message : 'Fundamentals sync failed.');
      }
    } finally {
      if (selectedInstrumentId.current === selectedInstrument.id) setFundamentalsSyncing(false);
    }
  }

  const displayFundamental = (value: string | null, suffix = '') =>
    value === null ? 'Unavailable' : `${Number(value).toLocaleString(undefined, { maximumFractionDigits: 4 })}${suffix}`;
  const eligibleSources = (marketDataSettings?.sources ?? []).filter(
    (source) => source.available && source.enabled,
  );
  return (
              <section className="market-page">
                <section className="data-section">
                  <label>
                    Data source
                    <select
                      value={selectedProvider ?? eligibleSources[0]?.name ?? ''}
                      onChange={(event) => void selectMarketDataSource(event.target.value)}
                      disabled={marketDataSettingsLoading || eligibleSources.length === 0}
                    >
                      {eligibleSources.map((source) => (
                        <option key={source.name} value={source.name}>{source.label}</option>
                      ))}
                    </select>
                  </label>
                  {marketDataSettingsError && <p className="market-source-message error" role="alert">{marketDataSettingsError}</p>}
                </section>
                <Panel
                  title="Market data coverage"
                  description="Stored real daily bars and the latest synchronization result for holdings and watchlist instruments."
                  className="market-coverage-panel"
                  action={(
                    <button
                      type="button"
                      onClick={() => void runBackfill()}
                      disabled={coverageBatchRunning || coverageRunning !== null || coverage.length === 0}
                    >
                      {coverageBatchRunning ? 'Backfilling…' : 'Backfill All Holdings'}
                    </button>
                  )}
                >
                  {coverageMessage && <p className="coverage-message" role="status">{coverageMessage}</p>}
                  {coverageLoading ? <Skeleton rows={4} /> : coverageError ? (
                    <ErrorState title="Coverage unavailable" message={coverageError} onRetry={() => void loadCoverage()} />
                  ) : coverage.length === 0 ? (
                    <EmptyState
                      title="No linked instruments"
                      message="Add an instrument to a portfolio holding or watchlist to track its history."
                    />
                  ) : (
                    <DataTable
                      columns={coverageColumns}
                      rows={coverage}
                      getRowKey={(instrument) => instrument.instrument_id}
                    />
                  )}
                </Panel>
                <div className="content-grid market-grid">
                  <section className="data-section">
                    <div className="section-heading"><div><p className="eyebrow">INSTRUMENT MASTER</p><h2>Search instruments</h2></div><Search size={18} /></div>
                    <form className="market-search" onSubmit={searchMarketInstruments}>
                      <label>Symbol, name, or venue<input value={marketQuery} onChange={(event) => setMarketQuery(event.target.value)} minLength={1} maxLength={80} placeholder="AAPL, RELIANCE, BTCUSD" required /></label>
                      <button className="primary-button" type="submit" disabled={searchingMarket}><Search size={15} />{searchingMarket ? 'Searching…' : 'Search'}</button>
                    </form>
                    {marketInstruments.length === 0 ? <div className="table-empty">{searchingMarket ? 'Searching instrument master…' : 'Search available equities and crypto markets.'}</div> : (
                      <div className="market-instrument-list" aria-label="Instrument search results">
                        {marketInstruments.map((instrument) => {
                          const isWatchlisted = watchlist.some((item) => item.instrument_id === instrument.id);
                          return <div className="market-instrument-result" key={instrument.id}>
                            <button className={selectedInstrument?.id === instrument.id ? 'market-instrument selected' : 'market-instrument'} type="button" onClick={() => void selectMarketInstrument(instrument)}>
                              <span><strong>{instrument.symbol}</strong><small>{instrument.display_name}</small></span><span className="market-instrument-meta">{instrument.venue} · {instrument.currency}<small>{instrument.asset_class}</small></span>
                            </button>
                            <button className="secondary-button" type="button" onClick={() => void (isWatchlisted ? removeInstrumentFromWatchlist(instrument.id) : addInstrumentToWatchlist(instrument.id, instrument.symbol))}>{isWatchlisted ? 'Remove' : 'Add to watchlist'}</button>
                          </div>;
                        })}
                      </div>
                    )}
                  </section>
                  <section className="entry-section market-controls">
                    <div className="section-heading"><div><p className="eyebrow">HISTORICAL BARS</p><h2>{selectedInstrument ? `${selectedInstrument.symbol} · ${selectedInstrument.venue}` : 'Select an instrument'}</h2></div><Clock3 size={18} /></div>
                    {selectedInstrument ? <>
                      <div className="market-instrument-details">
                        <p>{selectedInstrument.display_name}</p>
                        <span>{selectedInstrument.asset_class.toUpperCase()} · {selectedInstrument.country} · {selectedInstrument.currency} · {selectedInstrument.timezone}</span>
                        {selectedInstrument.is_synthetic && <strong className="synthetic-badge">SYNTHETIC DEMO INSTRUMENT</strong>}
                        {selectedInstrument.relink_required && <strong className="relink-warning">Portfolio holding needs relinking to a real instrument.</strong>}
                        {selectedInstrument.expiry && <span>Expiry {selectedInstrument.expiry}{selectedInstrument.strike ? ` · ${selectedInstrument.option_type} ${selectedInstrument.strike}` : ''}{selectedInstrument.multiplier ? ` · multiplier ${selectedInstrument.multiplier}` : ''}</span>}
                        {selectedInstrument.provider_identifiers.map((mapping) => <span key={`${mapping.provider}-${mapping.provider_instrument_id}`}>Provider ID: {mapping.provider}:{mapping.provider_instrument_id} · {mapping.is_verified ? 'verified mapping' : 'unverified mapping'}</span>)}
                      </div>
                      {summary?.positions.some((position) => position.symbol === selectedInstrument.symbol) && selectedInstrument.currency === currency && <button className="secondary-button" type="button" onClick={() => void attachSelectedInstrument()}>Link to portfolio holding</button>}
                      {watchlist.some((item) => item.instrument_id === selectedInstrument.id)
                        ? <button className="secondary-button" type="button" onClick={() => void removeInstrumentFromWatchlist(selectedInstrument.id)}>Remove from refresh watchlist</button>
                        : <button className="secondary-button" type="button" onClick={() => void addSelectedInstrumentToWatchlist()}>Add to refresh watchlist</button>}
                      <button className="secondary-button" type="button" onClick={() => void loadMarketQuote(selectedInstrument, true)} disabled={loadingMarketQuote}>{loadingMarketQuote ? 'Loading quote…' : 'Refresh instrument price'}</button>
                      {liveFeedStatus !== 'not_configured' && <p className="market-provenance" role="status">{selectedSourceLabel} live feed: {liveFeedStatus.replaceAll('_', ' ')}.{liveFeedMessage ? ` ${liveFeedMessage}` : ''}</p>}
                      {displayedMarketQuote && <div className="market-quote" aria-live="polite">
                        <div className="section-heading"><div><p className="eyebrow">{displayedMarketQuote.data_type === 'daily_close' ? 'HISTORICAL DAILY CLOSE' : displayedMarketQuote.data_type === 'live_tick' ? 'LIVE WEBSOCKET TICK' : displayedMarketQuote.data_type === 'provider_snapshot' ? 'LATEST REST SNAPSHOT' : 'LATEST PROVIDER QUOTE'}</p><h3>{formatMoney(displayedMarketQuote.bar.close, displayedMarketQuote.currency)}</h3></div><span className={`freshness-badge freshness-${displayedMarketQuote.freshness_status}`}>{displayedMarketQuote.freshness_status.replaceAll('_', ' ')}</span></div>
                        <p className="market-provenance">{displayedMarketQuote.freshness_message} Source: {displayedMarketQuote.source} · Market timestamp: {new Date(displayedMarketQuote.market_timestamp).toLocaleString()} · Observation age: {Math.floor(displayedMarketQuote.market_age_seconds)}s · Retrieved: {new Date(displayedMarketQuote.retrieved_at).toLocaleString()} · {displayedMarketQuote.is_cached ? 'Cached' : 'Provider response'} · {displayedMarketQuote.is_synthetic ? 'Synthetic' : displayedMarketQuote.is_verified ? 'Verified mapping' : 'Unverified mapping'}</p>
                        <p className="market-provenance">Provider configured: {displayedMarketQuote.provider_configured ? 'yes' : 'no'} · Delivery: {displayedMarketQuote.delivery_mode.replaceAll('_', ' ')} · Latest request: {displayedMarketQuote.provider_request_status.replaceAll('_', ' ')} · Persistent connection: {displayedMarketQuote.provider_connection_status.replaceAll('_', ' ')} · Stream active: {displayedMarketQuote.feed_active ? 'yes' : 'no'} · Provider delay: unavailable.</p>
                        {displayedMarketQuote.fallback_reason && <p className="market-provenance">Fallback reason: {displayedMarketQuote.fallback_reason.replaceAll('_', ' ')}.</p>}
                        {displayedMarketQuote.provider_attempts.length > 0 && <p className="market-provenance">Provider attempts: {displayedMarketQuote.provider_attempts.map((attempt) => `${attempt.provider}: ${attempt.outcome.replaceAll('_', ' ')}${attempt.reason_category ? ` [${attempt.reason_category.replaceAll('_', ' ')}]` : attempt.reason ? ` (${attempt.reason.replaceAll('_', ' ')})` : ''}${attempt.latency_ms !== null && attempt.latency_ms !== undefined ? `, ${Math.round(attempt.latency_ms)} ms` : ''}`).join(' · ')}</p>}
                      </div>}
                      <div className="market-range">
                          <label>Interval<select value={marketInterval} onChange={(event) => { const interval = event.target.value as typeof marketInterval; setMarketInterval(interval); setMarketHistory(null); if (selectedInstrument) void loadInstrumentIndicators(selectedInstrument, interval, marketHistory?.provider); }}><option value="1d">1 day</option><option value="1h">1 hour</option><option value="15m">15 minutes</option><option value="5m">5 minutes</option><option value="1m">1 minute</option></select></label>
                          <label>From<input type="date" value={marketStart} onChange={(event) => { setMarketStart(event.target.value); setMarketHistory(null); }} required /></label>
                          <label>Through<input type="date" value={marketEnd} onChange={(event) => { setMarketEnd(event.target.value); setMarketHistory(null); }} required /></label>
                      </div>
                      <button className="primary-button" type="button" onClick={loadMarketHistory} disabled={loadingMarketHistory}>{loadingMarketHistory ? 'Loading history…' : 'Load history'}</button>
                    </> : <p className="table-empty">Choose a search result to inspect instrument details and available bars.</p>}
                  </section>
                </div>
                {selectedInstrument && <section className="data-section instrument-detail-panel">
                  <div className="fundamentals-panel">
                    <div className="section-heading">
                      <div><p className="eyebrow">PROVIDER-REPORTED COMPANY DATA</p><h2>{selectedInstrument.symbol} fundamentals</h2></div>
                      <button className="secondary-button" type="button" onClick={() => void syncFundamentals()} disabled={fundamentalsSyncing || selectedInstrument.is_synthetic}>{fundamentalsSyncing ? 'Syncing…' : 'Sync fundamentals'}</button>
                    </div>
                    <p className="accounting-note">Currency: {selectedInstrument.currency}. Values are not converted. Missing provider fields are unavailable, never shown as zero.</p>
                    {fundamentalsMessage && <p className="market-provenance" role="status">{fundamentalsMessage}</p>}
                                {fundamentalsLoading ? <p className="loading-inline">Loading {selectedInstrument.symbol} fundamentals…</p> : fundamentals?.unavailable ? <p className="table-empty">{fundamentals.message} Use Sync fundamentals to request the selected instrument from Yahoo Finance. Missing fields remain unavailable.</p> : fundamentals ? <>
                      {fundamentals.snapshot && <>
                        <div className="fundamental-snapshot-grid">
                          <article><span>Market cap</span><strong>{fundamentals.snapshot.market_cap === null ? 'Unavailable' : formatMoney(fundamentals.snapshot.market_cap, selectedInstrument.currency)}</strong></article>
                          <article><span>Trailing P/E</span><strong>{displayFundamental(fundamentals.snapshot.pe)}</strong></article>
                          <article><span>Price / book</span><strong>{displayFundamental(fundamentals.snapshot.pb)}</strong></article>
                          <article><span>Dividend yield</span><strong>{fundamentals.snapshot.dividend_yield === null ? 'Unavailable' : `${(Number(fundamentals.snapshot.dividend_yield) * 100).toFixed(2)}%`}</strong></article>
                          <article><span>Sector</span><strong>{fundamentals.snapshot.sector || 'Unavailable'}</strong></article>
                          <article><span>Industry</span><strong>{fundamentals.snapshot.industry || 'Unavailable'}</strong></article>
                        </div>
                        <p className="market-provenance">
                          Source: {fundamentals.snapshot.source} · Fetched {new Date(fundamentals.snapshot.fetched_at).toLocaleString()} · <span className={`freshness-badge ${fundamentals.snapshot.stale ? 'freshness-stale' : 'freshness-fresh'}`}>{fundamentals.snapshot.stale ? 'stale' : 'fresh'}</span>
                        </p>
                      </>}
                      <div className="fundamental-period-toolbar">
                        <h3>Financial periods</h3>
                        <label>Period<select value={fundamentalPeriodType} onChange={(event) => setFundamentalPeriodType(event.target.value as 'annual' | 'quarterly')}><option value="annual">Annual</option><option value="quarterly">Quarterly</option></select></label>
                      </div>
                      {fundamentals.fundamentals.length === 0 ? <p className="table-empty">No financial periods are available for this instrument yet.</p> : <div className="table-scroll"><table><thead><tr><th>Period end</th><th>Revenue</th><th>YoY</th><th>Net income</th><th>YoY</th><th>Operating margin</th><th>ROE</th><th>EPS</th><th>YoY</th><th>Debt / equity</th><th>Operating cash flow</th><th>FCF</th><th>YoY</th><th>Source / fetched</th></tr></thead><tbody>{fundamentals.fundamentals.map((row) => <tr key={row.id}>
                        <td>{row.period_end}</td>
                        <td>{row.revenue === null ? 'Unavailable' : formatMoney(row.revenue, row.currency)}</td>
                        <td>{displayFundamental(row.revenue_yoy_growth_pct, '%')}</td>
                        <td>{row.net_income === null ? 'Unavailable' : formatMoney(row.net_income, row.currency)}</td>
                        <td>{displayFundamental(row.net_income_yoy_growth_pct, '%')}</td>
                        <td>{displayFundamental(row.operating_margin, '%')}</td>
                        <td>{displayFundamental(row.return_on_equity_pct, '%')}</td>
                        <td>{displayFundamental(row.eps)}</td>
                        <td>{displayFundamental(row.eps_yoy_growth_pct, '%')}</td>
                        <td>{displayFundamental(row.debt_to_equity)}</td>
                        <td>{row.operating_cash_flow === null ? 'Unavailable' : formatMoney(row.operating_cash_flow, row.currency)}</td>
                        <td>{row.free_cash_flow === null ? 'Unavailable' : formatMoney(row.free_cash_flow, row.currency)}</td>
                        <td>{displayFundamental(row.free_cash_flow_yoy_growth_pct, '%')}</td>
                        <td>{row.source}<small className="trade-subtext">{new Date(row.fetched_at).toLocaleString()} · <span className={row.stale ? 'stale-text' : ''}>{row.stale ? 'stale' : 'fresh'}</span></small></td>
                      </tr>)}</tbody></table></div>}
                    </> : <p className="table-empty">Fundamentals unavailable from providers.</p>}
                  </div>
                </section>}
                {marketHistory && selectedInstrument && <section className="data-section market-history">
                  <div className="section-heading"><div><p className="eyebrow">PROVENANCE &amp; FRESHNESS</p><h2>{selectedInstrument.symbol} history</h2></div><span className={`freshness-badge freshness-${marketHistory.freshness.status}`}>{marketHistory.freshness.status.replace('_', ' ')}</span></div>
                  <p className="market-provenance">{marketHistory.freshness.message} Provider: {marketHistory.provider ?? marketHistory.freshness.source ?? 'unavailable'} · Interval: {marketHistory.interval} · Currency: {marketHistory.currency ?? selectedInstrument.currency} · Session: {marketHistory.session_state} · Last successful fetch: {marketHistory.freshness.last_successful_update ? new Date(marketHistory.freshness.last_successful_update).toLocaleString() : 'Never'} · Retrieved: {marketHistory.fetched_at ? new Date(marketHistory.fetched_at).toLocaleString() : 'not recorded'} · Latest bar: {marketHistory.as_of ? new Date(marketHistory.as_of).toLocaleString() : marketHistory.freshness.latest_bar_timestamp ? marketInterval === '1d' ? new Date(marketHistory.freshness.latest_bar_timestamp).toISOString().slice(0, 10) : new Date(marketHistory.freshness.latest_bar_timestamp).toLocaleString() : 'Unavailable'} · {marketHistory.is_cached ? 'Cached' : 'Provider response'} · Sync: {marketHistory.sync_status.replaceAll('_', ' ')}</p>
                  {marketHistory.fallback_reason && <p className="market-provenance">Fallback reason: {marketHistory.fallback_reason.replaceAll('_', ' ')}.</p>}
                  {marketHistory.provider_attempts.length > 0 && <p className="market-provenance">Provider attempts: {marketHistory.provider_attempts.map((attempt) => `${attempt.provider}: ${attempt.outcome.replaceAll('_', ' ')}${attempt.reason_category ? ` [${attempt.reason_category.replaceAll('_', ' ')}]` : attempt.reason ? ` (${attempt.reason.replaceAll('_', ' ')})` : ''}${attempt.latency_ms !== null && attempt.latency_ms !== undefined ? `, ${Math.round(attempt.latency_ms)} ms` : ''}${attempt.bars ? `, ${attempt.bars} bars` : ''}`).join(' · ')}</p>}
                  {marketHistory.bars.length === 0 ? <div className="table-empty">No bars are available for that date range.</div> : <>
                    <CandleChart bars={marketHistory.bars} currency={selectedInstrument.currency} interval={marketInterval} preferredSource={marketIndicators?.source} indicatorSeries={marketIndicators?.indicator_series} indicatorBarsAvailable={marketIndicators?.bars_available} onLoadOlder={loadOlderMarketHistory} loadingOlder={loadingOlderMarketHistory} hasOlder={hasOlderMarketHistory} />
                    <details className="historical-bars-details"><summary>View stored OHLCV rows ({marketHistory.bars.length})</summary><div className="table-scroll"><table><thead><tr><th>{marketInterval === '1d' ? 'Session date' : 'Timestamp (UTC)'}</th><th>Open</th><th>High</th><th>Low</th><th>Close</th><th>Volume</th><th>Source</th></tr></thead><tbody>{marketHistory.bars.map((bar) => {
                      const verified = selectedInstrument.provider_identifiers.find((mapping) => mapping.provider === bar.source)?.is_verified === true;
                      const displayedTimestamp = marketInterval === '1d'
                        ? new Date(bar.timestamp).toISOString().slice(0, 10)
                        : new Date(bar.timestamp).toISOString().replace('T', ' ').replace('.000Z', 'Z');
                      return <tr key={`${bar.timestamp}-${bar.source}`}><td>{displayedTimestamp}</td><td>{Number(bar.open).toFixed(2)}</td><td>{Number(bar.high).toFixed(2)}</td><td>{Number(bar.low).toFixed(2)}</td><td>{Number(bar.close).toFixed(2)}</td><td>{bar.volume}</td><td>{bar.is_synthetic ? 'Synthetic · ' : ''}{bar.is_synthetic || !verified ? 'Unverified · ' : 'Verified · '}{bar.source}</td></tr>;
                    })}</tbody></table></div></details>
                  </>}
                </section>}
              </section>
  );
}
