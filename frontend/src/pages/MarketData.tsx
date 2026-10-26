import {
  ArrowUpRight, Clock3, Pencil, Plus, Search, ShieldCheck, Trash2, Wallet,
} from 'lucide-react';
import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react';
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
type NamedWatchlist = {
  id: string;
  name: string;
  instrument_count: number;
  created_at: string;
};
type WatchlistTab = 'Chart' | 'Info' | 'Financials' | 'Depth' | 'Options';
type MarketStreamStatus = 'not_configured' | 'connecting' | 'active' | 'reconnecting' | 'unavailable' | 'market_closed';
type MarketStreamMessage =
  | { type: 'status'; status: MarketStreamStatus; message?: string }
  | { type: 'tick'; quote: NonNullable<WorkspaceState['marketQuote']> };

export function MarketDataPage({ workspace }: { workspace: Pick<WorkspaceState, 'attachSelectedInstrument' | 'currency' | 'hasOlderMarketHistory' | 'loadInstrumentIndicators' | 'loadMarketHistory' | 'loadMarketQuote' | 'loadOlderMarketHistory' | 'loadingMarketHistory' | 'loadingOlderMarketHistory' | 'loadingMarketQuote' | 'marketEnd' | 'marketHistory' | 'marketIndicators' | 'marketInstruments' | 'marketInterval' | 'marketQuery' | 'marketQuote' | 'marketStart' | 'searchMarketInstruments' | 'searchingMarket' | 'selectMarketInstrument' | 'selectedInstrument' | 'setMarketEnd' | 'setMarketHistory' | 'setMarketInterval' | 'setMarketQuery' | 'setMarketStart' | 'summary' | 'valuation'> }) {
  const {
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
} = workspace;
  const [fundamentals, setFundamentals] = useState<FundamentalsData | null>(null);
  const [fundamentalsLoading, setFundamentalsLoading] = useState(false);
  const [fundamentalsSyncing, setFundamentalsSyncing] = useState(false);
  const [fundamentalsMessage, setFundamentalsMessage] = useState('');
  const [fundamentalPeriodType, setFundamentalPeriodType] = useState<'annual' | 'quarterly'>('annual');
  const selectedInstrumentId = useRef<string | null>(null);
  const dhanStreamRef = useRef<WebSocket | null>(null);
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
  const [namedWatchlists, setNamedWatchlists] = useState<NamedWatchlist[]>([]);
  const [activeWatchlistId, setActiveWatchlistId] = useState('');
  const [watchlistEntries, setWatchlistEntries] = useState<WorkspaceState['watchlist']>([]);
  const [watchlistLoading, setWatchlistLoading] = useState(true);
  const [watchlistError, setWatchlistError] = useState('');
  const [watchlistMessage, setWatchlistMessage] = useState('');
  const [newWatchlistName, setNewWatchlistName] = useState('');
  const [renameWatchlistName, setRenameWatchlistName] = useState('');
  const [activeWatchlistInstrument, setActiveWatchlistInstrument] = useState('');
  const [legacyInstrumentSelected, setLegacyInstrumentSelected] = useState(false);
  const [activeDetailTab, setActiveDetailTab] = useState<WatchlistTab>('Chart');
  const [watchlistInstrumentLoading, setWatchlistInstrumentLoading] = useState(false);
  const displayedMarketQuote = liveMarketQuote ?? marketQuote;
  const selectedProvider = marketDataSettings?.selected_provider;
  const selectedSourceLabel = marketDataSettings?.sources.find(
    (source) => source.name === selectedProvider,
  )?.label ?? 'Market-data';
  const liveStreamSupported = ['dhan', 'angel_one'].includes(
    marketDataSettings?.selected_provider ?? '',
  );
  const activeDetailInstrumentId = activeWatchlistInstrument
    || (legacyInstrumentSelected ? selectedInstrument?.id ?? '' : '');
  const activeDetailInstrumentIdRef = useRef(activeDetailInstrumentId);
  activeDetailInstrumentIdRef.current = activeDetailInstrumentId;
  const hasWatchlistInstruments = watchlistEntries.length > 0;
  const hasActiveDetailInstrument = Boolean(activeDetailInstrumentId);
  const watchlistInstrumentSignature = watchlistEntries
    .map((entry) => entry.instrument_id)
    .sort()
    .join(',');

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

  const refreshNamedWatchlists = useCallback(async (preferredId?: string) => {
    const lists = await apiRequest<NamedWatchlist[]>('/market-data/watchlists');
    setNamedWatchlists(lists);
    const storedId = window.localStorage.getItem('market-data-selected-watchlist');
    const nextId = [preferredId, storedId].find(
      (candidate) => candidate && lists.some((watchlist) => watchlist.id === candidate),
    ) ?? lists[0]?.id ?? '';
    setActiveWatchlistId(nextId);
    const selected = lists.find((watchlist) => watchlist.id === nextId);
    setRenameWatchlistName(selected?.name ?? '');
    return lists;
  }, []);

  const refreshSelectedWatchlist = useCallback(async (watchlistId = activeWatchlistId) => {
    if (!watchlistId) {
      setWatchlistEntries([]);
      setActiveWatchlistInstrument('');
      setWatchlistLoading(false);
      return;
    }
    setWatchlistLoading(true);
    setWatchlistError('');
    try {
      const entries = await apiRequest<WorkspaceState['watchlist']>(
        `/market-data/watchlist?watchlist_id=${encodeURIComponent(watchlistId)}`,
      );
      setWatchlistEntries(entries);
      setActiveWatchlistInstrument((current) =>
        entries.some((entry) => entry.instrument_id === current) ? current : '',
      );
    } catch (error) {
      setWatchlistError(error instanceof Error ? error.message : 'Watchlist could not be loaded.');
    } finally {
      setWatchlistLoading(false);
    }
  }, [activeWatchlistId]);

  useEffect(() => {
    void refreshNamedWatchlists().catch((error: Error) => {
      setWatchlistError(error.message);
      setWatchlistLoading(false);
    });
  }, [refreshNamedWatchlists]);

  useEffect(() => {
    if (!activeWatchlistId) return;
    window.localStorage.setItem('market-data-selected-watchlist', activeWatchlistId);
    void refreshSelectedWatchlist(activeWatchlistId);
  }, [activeWatchlistId, refreshSelectedWatchlist]);

  async function createNamedWatchlist(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const name = newWatchlistName.trim();
    if (!name) return;
    setWatchlistError('');
    try {
      const created = await apiRequest<NamedWatchlist>('/market-data/watchlists', {
        method: 'POST',
        body: JSON.stringify({ name }),
      });
      setNewWatchlistName('');
      await refreshNamedWatchlists(created.id);
    } catch (error) {
      setWatchlistError(error instanceof Error ? error.message : 'Watchlist could not be created.');
    }
  }

  async function renameNamedWatchlist(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!activeWatchlistId || !renameWatchlistName.trim()) return;
    setWatchlistError('');
    try {
      await apiRequest<NamedWatchlist>(`/market-data/watchlists/${activeWatchlistId}`, {
        method: 'PATCH',
        body: JSON.stringify({ name: renameWatchlistName.trim() }),
      });
      await refreshNamedWatchlists(activeWatchlistId);
    } catch (error) {
      setWatchlistError(error instanceof Error ? error.message : 'Watchlist could not be renamed.');
    }
  }

  async function deleteNamedWatchlist() {
    if (!activeWatchlistId) return;
    setWatchlistError('');
    try {
      await apiRequest<void>(`/market-data/watchlists/${activeWatchlistId}`, {
        method: 'DELETE',
      });
      setActiveWatchlistInstrument('');
      await refreshNamedWatchlists();
    } catch (error) {
      setWatchlistError(error instanceof Error ? error.message : 'Watchlist could not be deleted.');
    }
  }

  async function changeWatchlistInstrument(instrumentId: string, add: boolean, symbol: string) {
    if (!activeWatchlistId) return;
    setWatchlistError('');
    try {
      await apiRequest<void>(
        `/market-data/watchlists/${activeWatchlistId}/instruments/${instrumentId}`,
        { method: add ? 'POST' : 'DELETE' },
      );
      await Promise.all([
        refreshSelectedWatchlist(activeWatchlistId),
        refreshNamedWatchlists(activeWatchlistId),
      ]);
      setWatchlistMessage(`${symbol} ${add ? 'added to' : 'removed from'} ${namedWatchlists.find((item) => item.id === activeWatchlistId)?.name ?? 'watchlist'}.`);
    } catch (error) {
      setWatchlistError(error instanceof Error ? error.message : 'Watchlist could not be updated.');
    }
  }

  async function openWatchlistInstrument(instrumentId: string) {
    setActiveWatchlistInstrument(instrumentId);
    setLegacyInstrumentSelected(false);
    setActiveDetailTab('Chart');
    setWatchlistInstrumentLoading(true);
    setWatchlistError('');
    try {
      const instrument = await apiRequest<WorkspaceState['selectedInstrument']>(
        `/market-data/instruments/${instrumentId}`,
      );
      if (!instrument) throw new Error('Instrument details are unavailable.');
      await selectMarketInstrument(instrument);
    } catch (error) {
      setWatchlistError(error instanceof Error ? error.message : 'Instrument details could not be loaded.');
    } finally {
      setWatchlistInstrumentLoading(false);
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
    if (marketDataSettings?.selected_provider === 'dhan') return;
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
    let marketClosed = false;
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
        marketClosed = message.status === 'market_closed';
        setLiveFeedStatus(message.status);
        if (message.status === 'unavailable') {
          setLiveFeedMessage(`${selectedSourceLabel} live feed is unavailable. Check the connection, permissions, and instrument mapping.`);
        }
        return;
      }
      if (selectedInstrumentId.current === message.quote.instrument_id) {
        setLiveMarketQuote(message.quote);
      }
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

  useEffect(() => {
    if (
      !liveFeedEnabled
      || marketDataSettings?.selected_provider !== 'dhan'
      || !activeWatchlistId
      || (!hasWatchlistInstruments && !hasActiveDetailInstrument)
    ) {
      if (marketDataSettings?.selected_provider === 'dhan' && !liveFeedEnabled) {
        setLiveMarketQuote(null);
        setLiveFeedStatus('not_configured');
        setLiveFeedMessage('');
      }
      return;
    }
    const streamUrl = new URL(
      `/api/v1/market-data/watchlists/${activeWatchlistId}/stream`,
      window.location.href,
    );
    streamUrl.protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    let intentionalClose = false;
    let marketClosed = false;
    let retryTimer: number | undefined;
    setLiveMarketQuote(null);
    setLiveFeedStatus('connecting');
    setLiveFeedMessage('');
    const socket = new WebSocket(streamUrl);
    dhanStreamRef.current = socket;
    const heartbeat = window.setInterval(() => {
      if (socket.readyState === WebSocket.OPEN) socket.send('ping');
    }, 15_000);
    socket.onopen = () => {
      socket.send(JSON.stringify({
        type: 'subscriptions',
        detail_instrument_id: activeDetailInstrumentIdRef.current || null,
      }));
    };
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
        marketClosed = message.status === 'market_closed';
        setLiveFeedStatus(message.status);
        setLiveFeedMessage(message.message ?? (
          message.status === 'unavailable'
            ? 'Dhan live feed is unavailable. Check the connection and instrument mapping.'
            : ''
        ));
        if (message.status === 'market_closed') {
          setLiveMarketQuote(null);
          void apiRequest<WorkspaceState['watchlist']>(
            `/market-data/watchlist?watchlist_id=${encodeURIComponent(activeWatchlistId)}`,
          ).then(setWatchlistEntries).catch((error: Error) => setWatchlistError(error.message));
        }
        return;
      }
      if (selectedInstrumentId.current === message.quote.instrument_id) {
        setLiveMarketQuote(message.quote);
      }
      setLiveFeedStatus('active');
      setLiveFeedMessage('');
      setWatchlistEntries((entries) => entries.map((entry) => {
        if (entry.instrument_id !== message.quote.instrument_id) return entry;
        return {
          ...entry,
          quote: {
            source: message.quote.source,
            retrieved_at: message.quote.retrieved_at,
            freshness: message.quote.freshness,
            freshness_status: message.quote.freshness_status,
            is_synthetic: message.quote.is_synthetic,
            is_verified: message.quote.is_verified,
            market_timestamp: message.quote.market_timestamp,
            market_age_seconds: message.quote.market_age_seconds,
            data_type: message.quote.data_type,
            close: message.quote.bar.close,
            absolute_change: message.quote.absolute_change,
            percent_change: message.quote.percent_change,
          },
        };
      }));
    };
    socket.onerror = () => {
      setLiveFeedStatus('unavailable');
      setLiveFeedMessage('The Dhan live feed connection failed.');
    };
    socket.onclose = (event) => {
      if (!intentionalClose && event.code === 1000 && marketClosed) {
        retryTimer = window.setTimeout(() => setStreamRetryKey((key) => key + 1), 30_000);
      } else if (!intentionalClose && ![4401, 4403, 4404, 1000].includes(event.code)) {
        setLiveFeedStatus('reconnecting');
        setLiveFeedMessage('Dhan live feed disconnected. Reconnecting…');
        retryTimer = window.setTimeout(() => setStreamRetryKey((key) => key + 1), 2000);
      }
    };
    return () => {
      intentionalClose = true;
      window.clearInterval(heartbeat);
      if (retryTimer !== undefined) window.clearTimeout(retryTimer);
      if (dhanStreamRef.current === socket) dhanStreamRef.current = null;
      socket.close();
    };
  }, [liveFeedEnabled, marketDataSettings?.selected_provider, activeWatchlistId, hasWatchlistInstruments, hasActiveDetailInstrument, streamRetryKey]);

  useEffect(() => {
    const socket = dhanStreamRef.current;
    if (
      marketDataSettings?.selected_provider !== 'dhan'
      || socket?.readyState !== WebSocket.OPEN
    ) return;
    socket.send(JSON.stringify({
      type: 'subscriptions',
      detail_instrument_id: activeDetailInstrumentId || null,
    }));
  }, [activeWatchlistId, activeDetailInstrumentId, watchlistInstrumentSignature, marketDataSettings?.selected_provider, liveFeedEnabled]);

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
  const financialsFetchedAt = fundamentals?.snapshot?.fetched_at
    ?? (fundamentals?.fundamentals.reduce(
      (latest, row) => !latest || row.fetched_at > latest ? row.fetched_at : latest,
      '',
    ) || null);
  const eligibleSources = (marketDataSettings?.sources ?? []).filter(
    (source) => source.available && source.enabled,
  );
  const selectedList = namedWatchlists.find((watchlist) => watchlist.id === activeWatchlistId);
  const activeWatchlistEntry = watchlistEntries.find(
    (entry) => entry.instrument_id === activeWatchlistInstrument,
  );
  const detailQuote = activeWatchlistEntry?.quote
    ?? (displayedMarketQuote && displayedMarketQuote.instrument_id === activeWatchlistInstrument
      ? displayedMarketQuote
      : null);
  const detailPrice = activeWatchlistEntry?.quote?.close
    ?? (displayedMarketQuote?.instrument_id === activeWatchlistInstrument
      ? displayedMarketQuote.bar.close
      : null);
  const detailLiveBar = liveFeedStatus === 'active'
    && liveMarketQuote?.instrument_id === activeWatchlistInstrument
    ? { ...liveMarketQuote.bar, source: liveMarketQuote.source }
    : null;
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
                <section className="watchlist-master-detail" aria-label="Watchlists and instrument details">
                  <aside className="watchlist-rail data-section">
                    <div className="section-heading">
                      <div><p className="eyebrow">LOCAL WATCHLISTS</p><h2>Markets to watch</h2></div>
                      <span className="section-count">{watchlistEntries.length} instruments</span>
                    </div>
                    <div className="watchlist-management">
                      <label>Watchlist
                        <select
                          value={activeWatchlistId}
                          onChange={(event) => {
                            setActiveWatchlistId(event.target.value);
                            setActiveWatchlistInstrument('');
                            setLegacyInstrumentSelected(false);
                          }}
                        >
                          {namedWatchlists.map((watchlist) => (
                            <option value={watchlist.id} key={watchlist.id}>{watchlist.name}</option>
                          ))}
                        </select>
                      </label>
                      <form onSubmit={(event) => void createNamedWatchlist(event)}>
                        <label className="visually-hidden" htmlFor="new-watchlist-name">New watchlist name</label>
                        <input
                          id="new-watchlist-name"
                          value={newWatchlistName}
                          onChange={(event) => setNewWatchlistName(event.target.value)}
                          maxLength={64}
                          placeholder="New watchlist"
                        />
                        <button type="submit" aria-label="Create watchlist" disabled={!newWatchlistName.trim()}>
                          <Plus size={15} />
                        </button>
                      </form>
                      {activeWatchlistId && (
                        <form onSubmit={(event) => void renameNamedWatchlist(event)}>
                          <label className="visually-hidden" htmlFor="rename-watchlist-name">Rename watchlist</label>
                          <input
                            id="rename-watchlist-name"
                            value={renameWatchlistName}
                            onChange={(event) => setRenameWatchlistName(event.target.value)}
                            maxLength={64}
                            aria-label="Rename selected watchlist"
                          />
                          <button type="submit" aria-label="Save watchlist name" disabled={!renameWatchlistName.trim()}>
                            <Pencil size={14} />
                          </button>
                          <button type="button" aria-label="Delete selected watchlist" onClick={() => void deleteNamedWatchlist()}>
                            <Trash2 size={14} />
                          </button>
                        </form>
                      )}
                    </div>
                    {watchlistError && <p className="market-source-message error" role="alert">{watchlistError}</p>}
                    {watchlistMessage && <p className="market-source-message" role="status">{watchlistMessage}</p>}
                    {watchlistLoading ? <p className="table-empty">Loading stored quotes…</p> : watchlistEntries.length === 0 ? (
                      <p className="table-empty">This watchlist is empty. Search an instrument below to add it.</p>
                    ) : (
                      <div className="watchlist-instrument-list" aria-label={`${selectedList?.name ?? 'Selected'} instruments`}>
                        <div className="watchlist-row watchlist-row-heading">
                          <span>Instrument</span><span>Last</span><span>Change</span><span>%</span>
                        </div>
                        {watchlistEntries.map((entry) => {
                          const change = entry.quote?.absolute_change ?? null;
                          const changeClass = change === null ? '' : Number(change) >= 0 ? 'positive' : 'negative';
                          return (
                            <div className="watchlist-row" key={entry.instrument_id}>
                              <button
                                type="button"
                                className={`watchlist-instrument-button${activeWatchlistInstrument === entry.instrument_id ? ' selected' : ''}`}
                                onClick={() => void openWatchlistInstrument(entry.instrument_id)}
                              >
                                <span className="watchlist-symbol">
                                  <strong>{entry.symbol}</strong>
                                  <small>{entry.venue}</small>
                                </span>
                                <span className="watchlist-number">{entry.quote ? formatMoney(entry.quote.close, entry.currency) : '—'}</span>
                                <span className={`watchlist-number ${changeClass}`}>
                                  {change === null ? '—' : formatMoney(change, entry.currency)}
                                </span>
                                <span className={`watchlist-number ${changeClass}`}>
                                  {entry.quote?.percent_change === null || entry.quote?.percent_change === undefined
                                    ? '—'
                                    : `${Number(entry.quote.percent_change).toFixed(2)}%`}
                                </span>
                              </button>
                              <button
                                className="watchlist-remove"
                                type="button"
                                aria-label={`Remove ${entry.symbol} from ${selectedList?.name ?? 'watchlist'}`}
                                onClick={() => void changeWatchlistInstrument(entry.instrument_id, false, entry.symbol)}
                              ><Trash2 size={13} /></button>
                            </div>
                          );
                        })}
                      </div>
                    )}
                  </aside>
                  <section className="watchlist-detail data-section">
                    <header className="watchlist-detail-header">
                      <div>
                        <p className="eyebrow">INSTRUMENT DETAIL</p>
                        <h2>{activeWatchlistEntry?.symbol ?? 'Select an instrument'}</h2>
                        <span>{activeWatchlistEntry?.venue ?? ''}</span>
                      </div>
                      <div className="watchlist-detail-price">
                        <strong>
                          {detailPrice
                            ? formatMoney(detailPrice, activeWatchlistEntry?.currency ?? selectedInstrument?.currency ?? 'INR')
                            : '—'}
                        </strong>
                        {activeWatchlistEntry?.quote?.absolute_change !== null
                          && activeWatchlistEntry?.quote?.absolute_change !== undefined
                          && activeWatchlistEntry.quote.percent_change !== null
                          && activeWatchlistEntry.quote.percent_change !== undefined && (
                            <small className={Number(activeWatchlistEntry.quote.absolute_change) >= 0 ? 'positive' : 'negative'}>
                              {formatMoney(activeWatchlistEntry.quote.absolute_change, activeWatchlistEntry.currency)} · {Number(activeWatchlistEntry.quote.percent_change).toFixed(2)}%
                            </small>
                          )}
                        {detailQuote && <small>
                          {detailQuote.source} · {detailQuote.freshness_status.replaceAll('_', ' ')} · {new Date(detailQuote.retrieved_at).toLocaleString()}
                        </small>}
                        {liveStreamSupported && (
                          <button
                            className="secondary-button"
                            type="button"
                            onClick={() => setLiveFeedEnabled((enabled) => !enabled)}
                          >{liveFeedEnabled ? 'Pause live feed' : 'Start live feed'}</button>
                        )}
                        {liveFeedEnabled && liveFeedStatus !== 'not_configured' && (
                          <small role="status">
                            Live feed: {liveFeedStatus.replaceAll('_', ' ')}{liveFeedMessage ? ` · ${liveFeedMessage}` : ''}
                          </small>
                        )}
                      </div>
                    </header>
                    <nav className="watchlist-detail-tabs" aria-label="Instrument detail">
                      {(['Chart', 'Info', 'Financials', 'Depth', 'Options'] as const).map((tab) => (
                        <button
                          key={tab}
                          type="button"
                          className={activeDetailTab === tab ? 'active' : ''}
                          aria-current={activeDetailTab === tab ? 'page' : undefined}
                          onClick={() => setActiveDetailTab(tab)}
                        >{tab}</button>
                      ))}
                    </nav>
                    {activeDetailTab === 'Chart' ? (
                      <div className="watchlist-chart-content">
                        {watchlistInstrumentLoading ? <p className="table-empty">Loading chart and stored quote…</p>
                          : !activeWatchlistInstrument ? <p className="table-empty">Choose an instrument from the watchlist.</p>
                            : marketHistory?.bars.length && selectedInstrument?.id === activeWatchlistInstrument ? (
                              <CandleChart
                                bars={marketHistory.bars}
                                liveBar={detailLiveBar}
                                currency={selectedInstrument.currency}
                                interval={marketInterval}
                                preferredSource={marketIndicators?.source}
                                indicatorSeries={marketIndicators?.indicator_series}
                                indicatorBarsAvailable={marketIndicators?.bars_available}
                                onLoadOlder={loadOlderMarketHistory}
                                loadingOlder={loadingOlderMarketHistory}
                                hasOlder={hasOlderMarketHistory}
                              />
                            ) : <p className="table-empty">No stored bars are available for this instrument and range.</p>}
                      </div>
                    ) : activeDetailTab === 'Financials' ? (
                      <div className="watchlist-financial-content">
                        <div className="section-heading">
                          <div>
                            <p className="eyebrow">COMPANY FUNDAMENTALS</p>
                            <h3>{selectedInstrument?.symbol ?? activeWatchlistEntry?.symbol ?? 'Financials'}</h3>
                          </div>
                          <button
                            className="secondary-button"
                            type="button"
                            onClick={() => void syncFundamentals()}
                            disabled={!selectedInstrument || fundamentalsSyncing || selectedInstrument.is_synthetic}
                          >{fundamentalsSyncing ? 'Syncing…' : 'Refresh from Yahoo Finance'}</button>
                        </div>
                        <p className="market-provenance">
                          Source: Yahoo Finance · Last updated: {financialsFetchedAt
                            ? new Date(financialsFetchedAt).toLocaleString()
                            : 'Not available'}
                        </p>
                        {fundamentalsMessage && <p className="market-provenance" role="status">{fundamentalsMessage}</p>}
                        {fundamentalsLoading ? <p className="loading-inline">Loading financials…</p>
                          : !selectedInstrument ? <p className="table-empty">Select an instrument to load its Yahoo Finance financials.</p>
                            : fundamentals?.unavailable ? <p className="table-empty">{fundamentals.message} Financials are unavailable; no values are inferred.</p>
                              : fundamentals && fundamentals.fundamentals.length > 0 ? (
                                <div className="table-scroll">
                                  <table>
                                    <thead><tr><th>Period end</th><th>Revenue</th><th>Net income</th><th>Operating margin</th><th>EPS</th><th>Free cash flow</th><th>Source / updated</th></tr></thead>
                                    <tbody>{fundamentals.fundamentals.map((row) => (
                                      <tr key={row.id}>
                                        <td>{row.period_end}</td>
                                        <td>{row.revenue === null ? '—' : formatMoney(row.revenue, row.currency)}</td>
                                        <td>{row.net_income === null ? '—' : formatMoney(row.net_income, row.currency)}</td>
                                        <td>{row.operating_margin === null ? '—' : displayFundamental(row.operating_margin, '%')}</td>
                                        <td>{row.eps === null ? '—' : displayFundamental(row.eps)}</td>
                                        <td>{row.free_cash_flow === null ? '—' : formatMoney(row.free_cash_flow, row.currency)}</td>
                                        <td>{row.source}<small className="trade-subtext">{new Date(row.fetched_at).toLocaleString()} · {row.stale ? 'stale' : 'fresh'}</small></td>
                                      </tr>
                                    ))}</tbody>
                                  </table>
                                </div>
                              ) : <p className="table-empty">No financial periods are available from Yahoo Finance for this instrument.</p>}
                      </div>
                    ) : (
                      <div className="watchlist-tab-placeholder">
                        <strong>{activeDetailTab} unavailable</strong>
                        <span>
                          {activeDetailTab === 'Info'
                            ? 'Source: Dhan Company Info · Last updated: Not available. No documented Dhan v2 company-info endpoint is available to this integration.'
                            : activeDetailTab === 'Depth'
                              ? 'Source: Dhan Market Depth · Last updated: Not available. Dhan documents depth as POST /marketfeed/quote with five levels; this integration is GET-only and does not return the requested 20 levels.'
                              : 'Source: Dhan Option Chain · Last updated: Not available. Dhan documents option-chain retrieval as POST; this integration is GET-only.'}
                        </span>
                      </div>
                    )}
                  </section>
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
                          const isWatchlisted = watchlistEntries.some((item) => item.instrument_id === instrument.id);
                          return <div className="market-instrument-result" key={instrument.id}>
                            <button className={selectedInstrument?.id === instrument.id ? 'market-instrument selected' : 'market-instrument'} type="button" onClick={() => { setActiveWatchlistInstrument(''); setLegacyInstrumentSelected(true); void selectMarketInstrument(instrument); }}>
                              <span><strong>{instrument.symbol}</strong><small>{instrument.display_name}</small></span><span className="market-instrument-meta">{instrument.venue} · {instrument.currency}<small>{instrument.asset_class}</small></span>
                            </button>
                            <button className="secondary-button" type="button" onClick={() => void changeWatchlistInstrument(instrument.id, !isWatchlisted, instrument.symbol)}>{isWatchlisted ? 'Remove' : 'Add to watchlist'}</button>
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
                      {watchlistEntries.some((item) => item.instrument_id === selectedInstrument.id)
                        ? <button className="secondary-button" type="button" onClick={() => void changeWatchlistInstrument(selectedInstrument.id, false, selectedInstrument.symbol)}>Remove from watchlist</button>
                        : <button className="secondary-button" type="button" onClick={() => void changeWatchlistInstrument(selectedInstrument.id, true, selectedInstrument.symbol)}>Add to watchlist</button>}
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
                {selectedInstrument && legacyInstrumentSelected && <section className="data-section instrument-detail-panel">
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
                {marketHistory && selectedInstrument && legacyInstrumentSelected && <section className="data-section market-history">
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
