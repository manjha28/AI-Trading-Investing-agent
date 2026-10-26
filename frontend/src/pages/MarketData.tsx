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
  | { type: 'status'; status: MarketStreamStatus; message?: string; last_snapshot_at?: string | null }
  | { type: 'tick'; quote: NonNullable<WorkspaceState['marketQuote']> };
type InstrumentMasterInfo = {
  isin: string | null;
  exchange: string | null;
  segment: string | null;
  instrument_type: string | null;
  lot_size: string | null;
  tick_size: string | null;
  listing_date: string | null;
  expiry: string | null;
  source: string;
};
type CompanyProfile = {
  instrument_id: string;
  symbol: string;
  status: 'available' | 'unavailable';
  name: string | null;
  sector: string | null;
  industry: string | null;
  description: string | null;
  market_cap: string | null;
  currency: string | null;
  source: string;
  fetched_at: string | null;
  message: string;
  instrument_master: InstrumentMasterInfo;
};
type DepthLevel = { quantity: number; orders: number; price: string };
type DepthMessage =
  | { type: 'status'; status: MarketStreamStatus; message?: string; last_snapshot_at?: string | null }
  | {
    type: 'depth';
    timestamp: string;
    last_snapshot_at: string;
    bids: DepthLevel[];
    asks: DepthLevel[];
  };
type OptionExpiryData = {
  instrument_id: string;
  symbol: string;
  expiries: string[];
  source: string;
  fetched_at: string;
};
type OptionGreeks = {
  delta: string | null;
  theta: string | null;
  gamma: string | null;
  vega: string | null;
};
type OptionContract = {
  security_id: string | null;
  last_price: string | null;
  oi: number | null;
  volume: number | null;
  top_ask_price: string | null;
  top_ask_quantity: number | null;
  top_bid_price: string | null;
  top_bid_quantity: number | null;
  greeks: OptionGreeks;
};
type OptionStrike = {
  strike: string;
  ce: OptionContract | null;
  pe: OptionContract | null;
};
type OptionChainData = {
  instrument_id: string;
  symbol: string;
  expiry: string;
  underlying_price: string | null;
  strikes: OptionStrike[];
  source: string;
  fetched_at: string;
};

export function MarketDataPage({ workspace, userId }: { userId: string; workspace: Pick<WorkspaceState, 'attachSelectedInstrument' | 'currency' | 'hasOlderMarketHistory' | 'loadInstrumentIndicators' | 'loadMarketHistory' | 'loadMarketQuote' | 'loadOlderMarketHistory' | 'loadingMarketHistory' | 'loadingOlderMarketHistory' | 'loadingMarketQuote' | 'marketEnd' | 'marketHistory' | 'marketIndicators' | 'marketInstruments' | 'marketInterval' | 'marketQuery' | 'marketQuote' | 'marketStart' | 'searchMarketInstruments' | 'searchingMarket' | 'selectMarketInstrument' | 'selectedInstrument' | 'setMarketEnd' | 'setMarketHistory' | 'setMarketInterval' | 'setMarketQuery' | 'setMarketStart' | 'summary' | 'valuation'> }) {
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
const loadIndicatorsRef = useRef(loadInstrumentIndicators);
loadIndicatorsRef.current = loadInstrumentIndicators;
  useEffect(() => {
    const countChartUpdate = () => {
      dhanDeliveryCountersRef.current.chartUpdatesApplied += 1;
    };
    window.addEventListener('market-chart-update-applied', countChartUpdate);
    const counterTimer = window.setInterval(() => {
      console.info(
        '[dhan-watchlist-feed] delivery counters D_messages_received_in_browser=%d '
        + 'E_chart_updates_applied=%d',
        dhanDeliveryCountersRef.current.messagesReceivedInBrowser,
        dhanDeliveryCountersRef.current.chartUpdatesApplied,
      );
      dhanDeliveryCountersRef.current = {
        messagesReceivedInBrowser: 0,
        chartUpdatesApplied: 0,
      };
    }, 10_000);
    return () => {
      window.removeEventListener('market-chart-update-applied', countChartUpdate);
      window.clearInterval(counterTimer);
    };
  }, []);
  function changeMarketInterval(
    interval: typeof marketInterval,
    shouldLoad = Boolean(selectedInstrument),
  ) {
    const lookbackDays: Record<typeof marketInterval, number> = {
      '1m': 3,
      '5m': 7,
      '15m': 30,
      '1h': 90,
      '1d': 14,
    };
    const end = new Date();
    const start = new Date(end);
    start.setDate(start.getDate() - lookbackDays[interval]);
    const formatDate = (date: Date) => [
      date.getFullYear(),
      String(date.getMonth() + 1).padStart(2, '0'),
      String(date.getDate()).padStart(2, '0'),
    ].join('-');
    const startDate = formatDate(start);
    const endDate = formatDate(end);
    setMarketInterval(interval);
    setMarketStart(startDate);
    setMarketEnd(endDate);
    if (selectedInstrument && shouldLoad) {
      void loadMarketHistory(interval, startDate, endDate);
    }
  }
  const [fundamentals, setFundamentals] = useState<FundamentalsData | null>(null);
  const [fundamentalsLoading, setFundamentalsLoading] = useState(false);
  const [fundamentalsSyncing, setFundamentalsSyncing] = useState(false);
  const [fundamentalsMessage, setFundamentalsMessage] = useState('');
  const [fundamentalPeriodType, setFundamentalPeriodType] = useState<'annual' | 'quarterly'>('annual');
  const selectedInstrumentId = useRef<string | null>(null);
  const dhanStreamRef = useRef<WebSocket | null>(null);
  const dhanConnectionGenerationRef = useRef(0);
  const dhanStreamCleanupTimerRef = useRef<number | undefined>(undefined);
  const intentionalWatchlistSocketsRef = useRef(new WeakSet<WebSocket>());
  const watchlistStreamReconcileRef = useRef<(() => void) | null>(null);
  const browserMessageTotalRef = useRef(0);
  const depthStreamRef = useRef<WebSocket | null>(null);
  const dhanDeliveryCountersRef = useRef({
    messagesReceivedInBrowser: 0,
    chartUpdatesApplied: 0,
  });
  selectedInstrumentId.current = selectedInstrument?.id ?? null;
  const [companyProfile, setCompanyProfile] = useState<CompanyProfile | null>(null);
  const [companyProfileLoading, setCompanyProfileLoading] = useState(false);
  const [companyProfileError, setCompanyProfileError] = useState('');
  const [optionExpiries, setOptionExpiries] = useState<string[]>([]);
  const [optionExpiryInstrumentId, setOptionExpiryInstrumentId] = useState<string | null>(null);
  const [selectedOptionExpiry, setSelectedOptionExpiry] = useState('');
  const [optionExpiriesLoading, setOptionExpiriesLoading] = useState(false);
  const [optionChain, setOptionChain] = useState<OptionChainData | null>(null);
  const [optionChainLoading, setOptionChainLoading] = useState(false);
  const [optionChainError, setOptionChainError] = useState('');
  const [depthStatus, setDepthStatus] = useState<MarketStreamStatus>('not_configured');
  const [depthMessage, setDepthMessage] = useState('');
  const [depthLastSnapshotAt, setDepthLastSnapshotAt] = useState<string | null>(null);
  const [depthSnapshot, setDepthSnapshot] = useState<Extract<DepthMessage, { type: 'depth' }> | null>(null);
  const [coverage, setCoverage] = useState<CoverageInstrument[]>([]);
  const [coverageLoading, setCoverageLoading] = useState(true);
  const [coverageError, setCoverageError] = useState('');
  const [coverageMessage, setCoverageMessage] = useState('');
  const [coverageRunning, setCoverageRunning] = useState<string | null>(null);
  const [coverageBatchRunning, setCoverageBatchRunning] = useState(false);
  const [liveFeedEnabled, setLiveFeedEnabled] = useState(false);
  const [liveFeedStatus, setLiveFeedStatus] = useState<MarketStreamStatus>('not_configured');
  const [liveFeedMessage, setLiveFeedMessage] = useState('');
  const [liveLastSnapshotAt, setLiveLastSnapshotAt] = useState<string | null>(null);
  const [streamRetryKey, setStreamRetryKey] = useState(0);
  const [liveMarketQuote, setLiveMarketQuote] = useState<NonNullable<WorkspaceState['marketQuote']> | null>(null);
  const [marketDataSettings, setMarketDataSettings] = useState<MarketDataSettings | null>(null);
  const [marketDataSettingsLoading, setMarketDataSettingsLoading] = useState(true);
  const [marketDataSettingsError, setMarketDataSettingsError] = useState('');
  const liveFeedEnabledRef = useRef(liveFeedEnabled);
  const selectedProviderRef = useRef(marketDataSettings?.selected_provider);
  liveFeedEnabledRef.current = liveFeedEnabled;
  selectedProviderRef.current = marketDataSettings?.selected_provider;
  const [namedWatchlists, setNamedWatchlists] = useState<NamedWatchlist[]>([]);
  const [activeWatchlistId, setActiveWatchlistId] = useState('');
  const activeWatchlistIdRef = useRef(activeWatchlistId);
  activeWatchlistIdRef.current = activeWatchlistId;
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
  const liveFeedLabel = liveFeedStatus === 'active'
    ? 'live'
    : liveFeedStatus.replaceAll('_', ' ');
  const selectedProvider = marketDataSettings?.selected_provider;
  const selectedSourceLabel = marketDataSettings?.sources.find(
    (source) => source.name === selectedProvider,
  )?.label ?? 'Market-data';
  const liveStreamSupported = ['dhan', 'angel_one'].includes(
    marketDataSettings?.selected_provider ?? '',
  );
  const activeDetailInstrumentId = activeWatchlistInstrument
    || (legacyInstrumentSelected ? selectedInstrument?.id ?? '' : '');
  const hasWatchlistInstruments = watchlistEntries.length > 0;
  const hasActiveDetailInstrument = Boolean(activeDetailInstrumentId);

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
    let connectionTimedOut = false;
    let retryTimer: number | undefined;
    let connectionTimer: number | undefined;
    setLiveMarketQuote(null);
    setLiveFeedStatus('connecting');
    setLiveFeedMessage('');
    const socket = new WebSocket(streamUrl);
    console.info('[market-feed] connect attempt url=%s', streamUrl.href);
    connectionTimer = window.setTimeout(() => {
      if (socket.readyState === WebSocket.CONNECTING || socket.readyState === WebSocket.OPEN) {
        connectionTimedOut = true;
        setLiveFeedStatus('unavailable');
        setLiveFeedMessage('Live feed connection timed out after 10 seconds.');
        socket.close(4008, 'Connection timed out');
      }
    }, 10_000);
    const heartbeat = window.setInterval(() => {
      if (socket.readyState === WebSocket.OPEN) socket.send('ping');
    }, 15_000);
    socket.onopen = () => {
      console.info('[market-feed] handshake result=accepted url=%s', streamUrl.href);
      if (connectionTimer !== undefined) window.clearTimeout(connectionTimer);
      connectionTimer = window.setTimeout(() => {
        if (socket.readyState === WebSocket.OPEN && !marketClosed) {
          connectionTimedOut = true;
          setLiveFeedStatus('unavailable');
          setLiveFeedMessage(`${selectedSourceLabel} did not confirm a live-feed subscription within 35 seconds.`);
          socket.close(4008, 'Provider handshake timed out');
        }
      }, 35_000);
    };
    socket.onmessage = (event) => {
      let message: MarketStreamMessage;
      try {
        message = JSON.parse(event.data) as MarketStreamMessage;
      } catch (error) {
        console.error('[market-feed] parse error', error instanceof Error ? error.message : 'invalid JSON');
        setLiveFeedStatus('unavailable');
        setLiveFeedMessage('The live feed returned an invalid message.');
        return;
      }
      if (message.type === 'status') {
        marketClosed = message.status === 'market_closed';
        setLiveLastSnapshotAt(message.last_snapshot_at ?? null);
        setLiveFeedStatus(message.status);
        if (message.status === 'active' || message.status === 'market_closed' || message.status === 'unavailable') {
          if (connectionTimer !== undefined) window.clearTimeout(connectionTimer);
        }
        setLiveFeedMessage(message.message ?? (
          message.status === 'unavailable'
            ? `${selectedSourceLabel} live feed is unavailable. Check the connection, permissions, and instrument mapping.`
            : ''
        ));
        return;
      }
      if (connectionTimer !== undefined) window.clearTimeout(connectionTimer);
      if (selectedInstrumentId.current === message.quote.instrument_id) {
        setLiveMarketQuote(message.quote);
      }
      setLiveFeedStatus('active');
      setLiveFeedMessage('');
    };
    socket.onerror = () => {
      console.error('[market-feed] socket error url=%s', streamUrl.href);
      if (!connectionTimedOut) {
        setLiveFeedStatus('unavailable');
        setLiveFeedMessage(`${selectedSourceLabel} live feed connection failed.`);
      }
    };
    socket.onclose = (event) => {
      console.info('[market-feed] close code=%d reason=%s', event.code, event.reason);
      if (connectionTimer !== undefined) window.clearTimeout(connectionTimer);
      if (!intentionalClose && !connectionTimedOut) {
        if (event.code === 1000 && marketClosed) {
          retryTimer = window.setTimeout(() => setStreamRetryKey((key) => key + 1), 30_000);
          return;
        }
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
      if (connectionTimer !== undefined) window.clearTimeout(connectionTimer);
      socket.close();
    };
  }, [liveFeedEnabled, liveStreamSupported, marketDataSettings?.selected_provider, selectedInstrument?.id, selectedSourceLabel, streamRetryKey]);

  useEffect(() => {
    if (!activeWatchlistId || !userId) return;
    const streamUrl = new URL(
      `/api/v1/market-data/watchlists/${activeWatchlistId}/stream`,
      window.location.href,
    );
    streamUrl.protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    let intentionalClose = false;
    let marketClosed = false;
    let connectionTimedOut = false;
    let retryTimer: number | undefined;
    let connectionTimer: number | undefined;
    let heartbeat: number | undefined;
    let socket: WebSocket | null = null;
    const generation = ++dhanConnectionGenerationRef.current;
    if (dhanStreamCleanupTimerRef.current !== undefined) {
      window.clearTimeout(dhanStreamCleanupTimerRef.current);
      dhanStreamCleanupTimerRef.current = undefined;
    }
    const handleFirstTickTimeout = () => {
      if (
        intentionalClose
        || connectionTimedOut
        || (socket?.readyState !== WebSocket.CONNECTING
          && socket?.readyState !== WebSocket.OPEN)
      ) return;
      connectionTimedOut = true;
      setLiveFeedStatus('unavailable');
      setLiveFeedMessage('Dhan did not deliver a live tick within 10 seconds.');
      socket?.close(4008, 'First live tick timed out');
    };

    const stop = () => {
      intentionalClose = true;
      if (heartbeat !== undefined) window.clearInterval(heartbeat);
      if (retryTimer !== undefined) window.clearTimeout(retryTimer);
      if (connectionTimer !== undefined) window.clearTimeout(connectionTimer);
      if (socket && socket.readyState !== WebSocket.CLOSED) {
        intentionalWatchlistSocketsRef.current.add(socket);
        socket.close(1000, 'Watchlist live feed paused');
      }
      if (socket && dhanStreamRef.current === socket) dhanStreamRef.current = null;
      if (
        !liveFeedEnabledRef.current
        && selectedProviderRef.current === 'dhan'
      ) {
        setLiveMarketQuote(null);
        setLiveFeedStatus('not_configured');
        setLiveFeedMessage('');
      }
    };

    const connect = () => {
      if (
        !liveFeedEnabledRef.current
        || selectedProviderRef.current !== 'dhan'
      ) return;
      const previousSocket = dhanStreamRef.current;
      if (
        previousSocket?.url === streamUrl.href
        && (
          previousSocket.readyState === WebSocket.CONNECTING
          || previousSocket.readyState === WebSocket.OPEN
        )
      ) {
        socket = previousSocket;
        return;
      }
      intentionalClose = false;
      connectionTimedOut = false;
      setLiveFeedStatus('connecting');
      setLiveFeedMessage('Connecting to Dhan; waiting for the first live tick.');
      const openAfterPreviousCloses = async () => {
        if (previousSocket && previousSocket.readyState !== WebSocket.CLOSED) {
          if (previousSocket.readyState !== WebSocket.CLOSING) {
          intentionalWatchlistSocketsRef.current.add(previousSocket);
          previousSocket.close(1000, 'Replacing watchlist stream');
          }
          await new Promise<void>((resolve) => {
          if (previousSocket.readyState === WebSocket.CLOSED) {
            resolve();
            return;
          }
          previousSocket.addEventListener('close', () => resolve(), { once: true });
          });
        }
        if (
          intentionalClose
          || connectionTimedOut
          || activeWatchlistIdRef.current !== activeWatchlistId
          || !liveFeedEnabledRef.current
          || selectedProviderRef.current !== 'dhan'
        ) return;

        socket = new WebSocket(streamUrl);
        dhanStreamRef.current = socket;
        console.info('[dhan-watchlist-feed] connect attempt url=%s', streamUrl.href);
        const activeSocket = socket;
        if (connectionTimer !== undefined) window.clearTimeout(connectionTimer);
        connectionTimer = window.setTimeout(handleFirstTickTimeout, 10_000);
        heartbeat = window.setInterval(() => {
          if (activeSocket.readyState === WebSocket.OPEN) activeSocket.send('ping');
        }, 15_000);
        activeSocket.onopen = () => {
          console.info(
          '[dhan-watchlist-feed] application handshake result=accepted url=%s',
          streamUrl.href,
          );
        };
        activeSocket.onmessage = (event) => {
          browserMessageTotalRef.current += 1;
          dhanDeliveryCountersRef.current.messagesReceivedInBrowser += 1;
          let message: MarketStreamMessage;
          try {
          message = JSON.parse(event.data) as MarketStreamMessage;
          } catch (error) {
          console.info(
            '[dhan-watchlist-feed] message received total=%d type=invalid',
            browserMessageTotalRef.current,
          );
          const reason = error instanceof Error ? error.message : 'invalid JSON';
          console.error('[dhan-watchlist-feed] parse error', reason);
          setLiveFeedStatus('unavailable');
          setLiveFeedMessage(`Invalid live-feed message: ${reason}`);
          return;
          }
          console.info(
          '[dhan-watchlist-feed] message received total=%d type=%s',
          browserMessageTotalRef.current,
          message.type === 'status' ? message.status : message.type,
          );
          if (message.type === 'status') {
          marketClosed = message.status === 'market_closed';
          setLiveLastSnapshotAt(message.last_snapshot_at ?? null);
          if (message.status === 'active') {
            if (connectionTimer !== undefined) window.clearTimeout(connectionTimer);
            setLiveFeedStatus('connecting');
            setLiveFeedMessage('Dhan is connected; waiting for the first live tick.');
          } else {
            setLiveFeedStatus(message.status);
            setLiveFeedMessage(message.message ?? (
              message.status === 'unavailable'
                ? 'Dhan live feed is unavailable.'
                : ''
            ));
          }
          if (
            message.status === 'market_closed'
            || message.status === 'unavailable'
          ) {
            if (connectionTimer !== undefined) window.clearTimeout(connectionTimer);
          }
          if (message.status === 'market_closed') {
            setLiveMarketQuote(null);
            void apiRequest<WorkspaceState['watchlist']>(
              `/market-data/watchlist?watchlist_id=${encodeURIComponent(activeWatchlistId)}`,
            ).then(setWatchlistEntries).catch((error: Error) => setWatchlistError(error.message));
          }
          return;
          }
          if (connectionTimer !== undefined) window.clearTimeout(connectionTimer);
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
        activeSocket.onerror = () => {
          console.error('[dhan-watchlist-feed] socket error url=%s', streamUrl.href);
          if (!connectionTimedOut) {
          setLiveFeedStatus('unavailable');
          setLiveFeedMessage('Dhan watchlist WebSocket reported a connection error.');
          }
        };
        activeSocket.onclose = (event) => {
          console.info(
          '[dhan-watchlist-feed] close code=%d reason=%s',
          event.code,
          event.reason,
          );
          if (dhanStreamRef.current === activeSocket) dhanStreamRef.current = null;
          if (connectionTimer !== undefined) window.clearTimeout(connectionTimer);
          const reason = event.reason || `WebSocket closed with code ${event.code}.`;
          if (
          intentionalClose
          || intentionalWatchlistSocketsRef.current.has(activeSocket)
          ) return;
          if (connectionTimedOut) {
          setLiveFeedStatus('reconnecting');
          setLiveFeedMessage('Dhan live feed timed out before delivering a tick; reconnecting.');
          retryTimer = window.setTimeout(connect, marketClosed ? 30_000 : 2000);
          return;
          }
          if ([4401, 4403, 4404].includes(event.code)) {
          setLiveFeedStatus('unavailable');
          setLiveFeedMessage(reason);
          } else {
          setLiveFeedStatus('reconnecting');
          setLiveFeedMessage(reason);
          retryTimer = window.setTimeout(connect, marketClosed ? 30_000 : 2000);
          }
        };
      };
      void openAfterPreviousCloses();
    };

    const reconcile = () => {
      if (liveFeedEnabledRef.current && selectedProviderRef.current === 'dhan') {
        connect();
      } else {
        stop();
      }
    };
    watchlistStreamReconcileRef.current = reconcile;
    reconcile();
    return () => {
      dhanStreamCleanupTimerRef.current = window.setTimeout(() => {
        if (generation === dhanConnectionGenerationRef.current) {
          intentionalClose = true;
          if (heartbeat !== undefined) window.clearInterval(heartbeat);
          if (retryTimer !== undefined) window.clearTimeout(retryTimer);
          if (connectionTimer !== undefined) window.clearTimeout(connectionTimer);
          if (socket && socket.readyState !== WebSocket.CLOSED) {
          intentionalWatchlistSocketsRef.current.add(socket);
          socket.close(1000, 'Watchlist stream effect cleanup');
          }
          if (dhanStreamRef.current === socket) dhanStreamRef.current = null;
          watchlistStreamReconcileRef.current = null;
        }
        dhanStreamCleanupTimerRef.current = undefined;
      }, 50);
    };
  }, [activeWatchlistId, userId]);

  useEffect(() => {
    watchlistStreamReconcileRef.current?.();
  }, [liveFeedEnabled, marketDataSettings?.selected_provider]);

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

  useEffect(() => {
    if (activeDetailTab !== 'Info' || !activeDetailInstrumentId) {
      setCompanyProfile(null);
      setCompanyProfileError('');
      setCompanyProfileLoading(false);
      return;
    }
    let active = true;
    setCompanyProfile(null);
    setCompanyProfileError('');
    setCompanyProfileLoading(true);
    apiRequest<CompanyProfile>(
      `/market-data/instruments/${activeDetailInstrumentId}/company-profile`,
    )
      .then((profile) => {
        if (active) setCompanyProfile(profile);
      })
      .catch((error: Error) => {
        if (active) setCompanyProfileError(error.message);
      })
      .finally(() => {
        if (active) setCompanyProfileLoading(false);
      });
    return () => {
      active = false;
    };
  }, [activeDetailTab, activeDetailInstrumentId]);

  useEffect(() => {
    if (
      marketDataSettings?.selected_provider !== 'dhan'
      || liveFeedStatus !== 'active'
      || activeDetailTab !== 'Chart'
      || !selectedInstrument
      || marketInterval === '1d'
    ) return;
    let active = true;
    let reconciling = false;
    const reconcile = async () => {
      if (reconciling) return;
      reconciling = true;
      const end = new Date();
      const start = new Date(end.getTime() - 2 * 24 * 60 * 60 * 1000);
      const params = new URLSearchParams({
        interval: marketInterval,
        start: start.toISOString(),
        end: end.toISOString(),
        limit: '500',
      });
      try {
        const refreshed = await apiRequest<NonNullable<WorkspaceState['marketHistory']>>(
          `/market-data/instruments/${selectedInstrument.id}/history/sync?${params}`,
          { method: 'POST' },
        );
        if (!active) return;
        setMarketHistory((current) => {
          if (!current) return refreshed;
          const barsByKey = new Map(
            [...current.bars, ...refreshed.bars].map((bar) => [
              `${bar.timestamp}-${bar.source}`,
              bar,
            ]),
          );
          return {
            ...refreshed,
            bars: [...barsByKey.values()].sort(
              (left, right) => new Date(left.timestamp).getTime() - new Date(right.timestamp).getTime(),
            ),
          };
        });
        await loadIndicatorsRef.current(
          selectedInstrument,
          marketInterval,
          refreshed.provider,
        );
      } catch (error) {
        if (active) console.warn('[market-chart] intraday reconciliation failed', error);
      } finally {
        reconciling = false;
      }
    };
    const timer = window.setInterval(() => void reconcile(), 60_000);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, [
    activeDetailTab,
    liveFeedStatus,
    marketDataSettings?.selected_provider,
    marketInterval,
    selectedInstrument,
    setMarketHistory,
  ]);

  useEffect(() => {
    if (activeDetailTab !== 'Options' || !activeDetailInstrumentId) {
      setOptionExpiries([]);
      setOptionExpiryInstrumentId(null);
      setSelectedOptionExpiry('');
      setOptionExpiriesLoading(false);
      setOptionChain(null);
      setOptionChainError('');
      setOptionChainLoading(false);
      return;
    }
    let active = true;
    setOptionExpiries([]);
    setOptionExpiryInstrumentId(null);
    setSelectedOptionExpiry('');
    setOptionExpiriesLoading(true);
    setOptionChain(null);
    setOptionChainError('');
    apiRequest<OptionExpiryData>(
      `/market-data/instruments/${activeDetailInstrumentId}/option-chain/expiries`,
    )
      .then((data) => {
        if (!active) return;
        setOptionExpiries(data.expiries);
        setOptionExpiryInstrumentId(data.instrument_id);
        setSelectedOptionExpiry(data.expiries[0] ?? '');
      })
      .catch((error: Error) => {
        if (active) setOptionChainError(error.message);
      })
      .finally(() => {
        if (active) setOptionExpiriesLoading(false);
      });
    return () => {
      active = false;
    };
  }, [activeDetailTab, activeDetailInstrumentId]);

  useEffect(() => {
    if (
      activeDetailTab !== 'Options'
      || !activeDetailInstrumentId
      || optionExpiryInstrumentId !== activeDetailInstrumentId
      || !selectedOptionExpiry
      || !optionExpiries.includes(selectedOptionExpiry)
    ) {
      setOptionChain(null);
      setOptionChainLoading(false);
      return;
    }
    let active = true;
    setOptionChain(null);
    setOptionChainError('');
    setOptionChainLoading(true);
    apiRequest<OptionChainData>(
      `/market-data/instruments/${activeDetailInstrumentId}/option-chain?expiry=${encodeURIComponent(selectedOptionExpiry)}`,
    )
      .then((data) => {
        if (active) setOptionChain(data);
      })
      .catch((error: Error) => {
        if (active) setOptionChainError(error.message);
      })
      .finally(() => {
        if (active) setOptionChainLoading(false);
      });
    return () => {
      active = false;
    };
  }, [
    activeDetailTab,
    activeDetailInstrumentId,
    optionExpiryInstrumentId,
    optionExpiries,
    selectedOptionExpiry,
  ]);

  useEffect(() => {
    if (
      activeDetailTab !== 'Depth'
      || !activeDetailInstrumentId
      || marketDataSettings?.selected_provider !== 'dhan'
    ) {
      setDepthStatus('not_configured');
      if (depthStreamRef.current?.readyState === WebSocket.OPEN) {
        depthStreamRef.current.send('unsubscribe');
      }
      return;
    }
    let intentionalClose = false;
    let timedOut = false;
    let providerActive = false;
    let marketClosed = false;
    let unavailable = false;
    let connectionTimer: number | undefined;
    const streamUrl = new URL(
      `/api/v1/market-data/instruments/${activeDetailInstrumentId}/depth`,
      window.location.href,
    );
    streamUrl.protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    setDepthStatus('connecting');
    setDepthMessage('');
    const socket = new WebSocket(streamUrl);
    depthStreamRef.current = socket;
    console.info('[dhan-depth-feed] connect attempt url=%s', streamUrl.href);
    connectionTimer = window.setTimeout(() => {
      if (socket.readyState === WebSocket.CONNECTING || socket.readyState === WebSocket.OPEN) {
        timedOut = true;
        setDepthStatus('unavailable');
        setDepthMessage('Dhan market-depth handshake timed out after 10 seconds.');
        socket.close(4008, 'Depth handshake timed out');
      }
    }, 10_000);
    socket.onopen = () => {
      console.info('[dhan-depth-feed] application handshake result=accepted url=%s', streamUrl.href);
      if (connectionTimer !== undefined) window.clearTimeout(connectionTimer);
      connectionTimer = window.setTimeout(() => {
        if (socket.readyState === WebSocket.OPEN && !providerActive && !marketClosed && !unavailable) {
          timedOut = true;
          setDepthStatus('unavailable');
          setDepthMessage('Dhan did not confirm the market-depth feed within 10 seconds.');
          socket.close(4008, 'Provider handshake timed out');
        }
      }, 10_000);
    };
    socket.onmessage = (event) => {
      let message: DepthMessage;
      try {
        message = JSON.parse(event.data) as DepthMessage;
      } catch (error) {
        console.error('[dhan-depth-feed] parse error', error instanceof Error ? error.message : 'invalid JSON');
        setDepthStatus('unavailable');
        setDepthMessage('The Dhan depth feed returned an invalid message.');
        return;
      }
      if (message.type === 'status') {
        providerActive = message.status === 'active';
        marketClosed = message.status === 'market_closed';
        unavailable = message.status === 'unavailable';
        setDepthStatus(message.status);
        setDepthMessage(message.message ?? '');
        setDepthLastSnapshotAt((current) => message.last_snapshot_at ?? current);
        if (message.status === 'active' || message.status === 'market_closed' || message.status === 'unavailable') {
          if (connectionTimer !== undefined) window.clearTimeout(connectionTimer);
        }
        return;
      }
      if (connectionTimer !== undefined) window.clearTimeout(connectionTimer);
      setDepthStatus('active');
      setDepthMessage('');
      setDepthSnapshot(message);
      setDepthLastSnapshotAt(message.last_snapshot_at);
    };
    socket.onerror = () => {
      console.error('[dhan-depth-feed] socket error url=%s', streamUrl.href);
      if (!timedOut) {
        unavailable = true;
        setDepthStatus('unavailable');
        setDepthMessage('Dhan market-depth connection failed.');
      }
    };
    socket.onclose = (event) => {
      console.info('[dhan-depth-feed] close code=%d reason=%s', event.code, event.reason);
      if (connectionTimer !== undefined) window.clearTimeout(connectionTimer);
      if (!intentionalClose && !timedOut && !marketClosed && !unavailable) {
        unavailable = true;
        setDepthStatus('unavailable');
        setDepthMessage(`Dhan market-depth feed closed (code ${event.code}${event.reason ? `: ${event.reason}` : ''}).`);
      }
    };
    return () => {
      intentionalClose = true;
      if (connectionTimer !== undefined) window.clearTimeout(connectionTimer);
      if (depthStreamRef.current === socket) depthStreamRef.current = null;
      if (socket.readyState === WebSocket.OPEN) socket.send('unsubscribe');
      socket.close(1000, 'Depth tab closed');
      console.info('[dhan-depth-feed] unsubscribe instrument=%s', activeDetailInstrumentId);
    };
  }, [activeDetailTab, activeDetailInstrumentId, marketDataSettings?.selected_provider]);

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
  const displayOptionPrice = (value: string | null) =>
    value === null ? '—' : formatMoney(value, 'INR');
  const displayOptionCount = (value: number | null) =>
    value === null ? '—' : value.toLocaleString();
  const displayOptionQuote = (price: string | null, quantity: number | null) =>
    price === null ? '—' : `${displayOptionPrice(price)} · ${displayOptionCount(quantity)}`;
  const displayOptionGreek = (value: string | null) =>
    value === null ? '—' : displayFundamental(value);
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
                            Live feed: {liveFeedLabel}{liveFeedMessage ? ` · ${liveFeedMessage}` : ''}
                            {liveFeedStatus === 'market_closed' && ` · Last snapshot: ${liveLastSnapshotAt ? new Date(liveLastSnapshotAt).toLocaleString() : 'Not available'}`}
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
                        <label className="market-chart-interval">
                          Interval
                          <select
                            value={marketInterval}
                            onChange={(event) => changeMarketInterval(
                              event.target.value as typeof marketInterval,
                              selectedInstrument?.id === activeWatchlistInstrument,
                            )}
                          >
                            <option value="1m">1 minute</option>
                            <option value="5m">5 minutes</option>
                            <option value="15m">15 minutes</option>
                            <option value="1h">60 minutes</option>
                            <option value="1d">1 day</option>
                          </select>
                        </label>
                        {watchlistInstrumentLoading ? <p className="table-empty">Loading chart and stored quote…</p>
                          : !activeWatchlistInstrument ? <p className="table-empty">Choose an instrument from the watchlist.</p>
                            : marketHistory?.bars.length && selectedInstrument?.id === activeWatchlistInstrument ? (
                              <CandleChart
                                bars={marketHistory.bars}
                                liveBar={detailLiveBar}
                                tickSize={selectedInstrument.tick_size}
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
                    ) : activeDetailTab === 'Info' ? (
                      <div className="watchlist-tab-placeholder">
                        <strong>{companyProfile?.name ?? activeWatchlistEntry?.display_name ?? activeWatchlistEntry?.symbol ?? 'Company profile'}</strong>
                        <span>
                          Source: {companyProfile?.instrument_master.source ?? 'Instrument record'} · Profile: Yahoo Finance · Last updated: {companyProfile?.fetched_at
                              ? new Date(companyProfile.fetched_at).toLocaleString()
                              : 'Not available'}
                        </span>
                        <span>ISIN: {companyProfile?.instrument_master.isin ?? 'Unavailable'}</span>
                        <span>Exchange: {companyProfile?.instrument_master.exchange ?? activeWatchlistEntry?.venue ?? 'Unavailable'}</span>
                        <span>Segment: {companyProfile?.instrument_master.segment ?? 'Unavailable'}</span>
                        <span>Instrument type: {companyProfile?.instrument_master.instrument_type ?? 'Unavailable'}</span>
                        <span>Lot size: {companyProfile?.instrument_master.lot_size ?? 'Unavailable'}</span>
                        <span>Tick size: {companyProfile?.instrument_master.tick_size ?? 'Unavailable'}</span>
                        {companyProfile?.instrument_master.listing_date && (
                          <span>Listing date: {companyProfile.instrument_master.listing_date}</span>
                        )}
                        {companyProfile?.instrument_master.expiry && (
                          <span>Expiry: {companyProfile.instrument_master.expiry}</span>
                        )}
                        {companyProfileLoading && <p className="loading-inline">Loading Yahoo Finance company profile…</p>}
                        {companyProfileError && <p className="market-source-message error" role="alert">{companyProfileError}</p>}
                        <span>Decision support, not investment advice.</span>
                        {companyProfile?.status === 'unavailable' && (
                          <p className="market-provenance" role="status">{companyProfile.message} Dhan v2 documentation checked; no company-info endpoint was found.</p>
                        )}
                        {companyProfile?.status === 'available' && (
                          <>
                            <span>Sector: {companyProfile.sector ?? 'Unavailable'}</span>
                            <span>Industry: {companyProfile.industry ?? 'Unavailable'}</span>
                            <span>Market cap: {companyProfile.market_cap === null
                              ? 'Unavailable'
                              : formatMoney(companyProfile.market_cap, companyProfile.currency ?? activeWatchlistEntry?.currency ?? 'INR')}</span>
                            <p>{companyProfile.description ?? 'Description unavailable from Yahoo Finance.'}</p>
                          </>
                        )}
                        {!companyProfileLoading && !companyProfile && !companyProfileError && (
                          <p className="table-empty">Open this instrument’s Info tab to request its Yahoo Finance profile.</p>
                        )}
                      </div>
                    ) : activeDetailTab === 'Depth' ? (
                      <div className="watchlist-financial-content">
                        <div className="section-heading">
                          <div>
                            <p className="eyebrow">LIVE ORDER BOOK</p>
                            <h3>{activeWatchlistEntry?.symbol ?? 'Market depth'}</h3>
                          </div>
                        </div>
                        <p className="market-provenance">
                          Source: Dhan 20-level WebSocket depth · Snapshot: {depthSnapshot
                            ? new Date(depthSnapshot.timestamp).toLocaleString()
                            : depthLastSnapshotAt
                              ? new Date(depthLastSnapshotAt).toLocaleString()
                              : detailQuote
                                ? new Date(detailQuote.retrieved_at).toLocaleString()
                                : 'Not available'}
                        </p>
                        <p className="market-provenance" role="status">
                          {depthStatus === 'market_closed'
                            ? `Market closed. Last known snapshot: ${depthLastSnapshotAt
                              ? new Date(depthLastSnapshotAt).toLocaleString()
                              : detailQuote
                                ? new Date(detailQuote.retrieved_at).toLocaleString()
                                : 'Not available'}.`
                            : depthStatus === 'not_configured'
                              ? 'Select Dhan as the market-data source and open this tab to subscribe.'
                              : depthMessage || `Market depth: ${depthStatus.replaceAll('_', ' ')}.`}
                        </p>
                        {depthSnapshot && (
                          <div className="table-scroll">
                            <table>
                              <thead><tr><th>Level</th><th>Bid qty</th><th>Bid orders</th><th>Bid price</th><th>Ask price</th><th>Ask orders</th><th>Ask qty</th></tr></thead>
                              <tbody>{Array.from({ length: 20 }, (_, index) => (
                                <tr key={index}>
                                  <td>{index + 1}</td>
                                  <td>{depthSnapshot.bids[index]?.quantity.toLocaleString() ?? '—'}</td>
                                  <td>{depthSnapshot.bids[index]?.orders.toLocaleString() ?? '—'}</td>
                                  <td>{depthSnapshot.bids[index] ? formatMoney(depthSnapshot.bids[index].price, activeWatchlistEntry?.currency ?? 'INR') : '—'}</td>
                                  <td>{depthSnapshot.asks[index] ? formatMoney(depthSnapshot.asks[index].price, activeWatchlistEntry?.currency ?? 'INR') : '—'}</td>
                                  <td>{depthSnapshot.asks[index]?.orders.toLocaleString() ?? '—'}</td>
                                  <td>{depthSnapshot.asks[index]?.quantity.toLocaleString() ?? '—'}</td>
                                </tr>
                              ))}</tbody>
                            </table>
                          </div>
                        )}
                        <p className="market-provenance">Dhan Full Market Depth is read-only and provides 20 levels for supported NSE equity instruments.</p>
                      </div>
                    ) : (
                      <div className="watchlist-financial-content">
                        <div className="section-heading">
                          <div>
                            <p className="eyebrow">OPTION CHAIN</p>
                            <h3>{activeWatchlistEntry?.symbol ?? 'Options'}</h3>
                          </div>
                          <label>
                            Expiry
                            <select
                              value={selectedOptionExpiry}
                              onChange={(event) => setSelectedOptionExpiry(event.target.value)}
                              disabled={optionExpiriesLoading || optionExpiries.length === 0}
                            >
                              {optionExpiries.map((expiry) => (
                                <option key={expiry} value={expiry}>{expiry}</option>
                              ))}
                            </select>
                          </label>
                        </div>
                        <p className="market-provenance">
                          Source: Dhan Option Chain
                          {optionChain?.fetched_at
                            ? ` · Updated: ${new Date(optionChain.fetched_at).toLocaleString()}`
                            : ''}
                          {optionChain?.underlying_price
                            ? ` · Underlying: ${displayOptionPrice(optionChain.underlying_price)}`
                            : ''}
                        </p>
                        <p className="market-provenance">
                          Read-only provider data · Decision support, not investment advice.
                          Dhan limits option-chain requests to one every 3 seconds.
                        </p>
                        {optionExpiriesLoading && <p className="loading-inline">Loading expiry list…</p>}
                        {optionChainLoading && <p className="loading-inline">Loading option chain…</p>}
                        {optionChainError && <p className="market-source-message error" role="alert">{optionChainError}</p>}
                        {!optionExpiriesLoading && !optionChainError && optionExpiries.length === 0 && (
                          <p className="table-empty">No active Dhan option expiries are available for this underlying.</p>
                        )}
                        {optionChain && optionChain.strikes.length > 0 && (
                          <div className="table-scroll">
                            <table>
                              <thead>
                                <tr>
                                  <th>Strike</th>
                                  <th>Call LTP</th><th>Call OI</th><th>Call volume</th><th>Call bid · qty</th><th>Call ask · qty</th>
                                  <th>Call Δ</th><th>Call Θ</th><th>Call Γ</th><th>Call Vega</th>
                                  <th>Put LTP</th><th>Put OI</th><th>Put volume</th><th>Put bid · qty</th><th>Put ask · qty</th>
                                  <th>Put Δ</th><th>Put Θ</th><th>Put Γ</th><th>Put Vega</th>
                                </tr>
                              </thead>
                              <tbody>
                                {optionChain.strikes.map((row) => (
                                  <tr key={row.strike}>
                                    <td>{displayOptionPrice(row.strike)}</td>
                                    <td>{displayOptionPrice(row.ce?.last_price ?? null)}</td>
                                    <td>{displayOptionCount(row.ce?.oi ?? null)}</td>
                                    <td>{displayOptionCount(row.ce?.volume ?? null)}</td>
                                    <td>{displayOptionQuote(row.ce?.top_bid_price ?? null, row.ce?.top_bid_quantity ?? null)}</td>
                                    <td>{displayOptionQuote(row.ce?.top_ask_price ?? null, row.ce?.top_ask_quantity ?? null)}</td>
                                    <td>{displayOptionGreek(row.ce?.greeks.delta ?? null)}</td>
                                    <td>{displayOptionGreek(row.ce?.greeks.theta ?? null)}</td>
                                    <td>{displayOptionGreek(row.ce?.greeks.gamma ?? null)}</td>
                                    <td>{displayOptionGreek(row.ce?.greeks.vega ?? null)}</td>
                                    <td>{displayOptionPrice(row.pe?.last_price ?? null)}</td>
                                    <td>{displayOptionCount(row.pe?.oi ?? null)}</td>
                                    <td>{displayOptionCount(row.pe?.volume ?? null)}</td>
                                    <td>{displayOptionQuote(row.pe?.top_bid_price ?? null, row.pe?.top_bid_quantity ?? null)}</td>
                                    <td>{displayOptionQuote(row.pe?.top_ask_price ?? null, row.pe?.top_ask_quantity ?? null)}</td>
                                    <td>{displayOptionGreek(row.pe?.greeks.delta ?? null)}</td>
                                    <td>{displayOptionGreek(row.pe?.greeks.theta ?? null)}</td>
                                    <td>{displayOptionGreek(row.pe?.greeks.gamma ?? null)}</td>
                                    <td>{displayOptionGreek(row.pe?.greeks.vega ?? null)}</td>
                                  </tr>
                                ))}
                              </tbody>
                            </table>
                          </div>
                        )}
                        {optionChain && optionChain.strikes.length === 0 && (
                          <p className="table-empty">Dhan returned no option strikes for this expiry.</p>
                        )}
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
                      {liveFeedStatus !== 'not_configured' && <p className="market-provenance" role="status">{selectedSourceLabel} live feed: {liveFeedLabel}.{liveFeedMessage ? ` ${liveFeedMessage}` : ''}</p>}
                      {displayedMarketQuote && <div className="market-quote" aria-live="polite">
                        <div className="section-heading"><div><p className="eyebrow">{displayedMarketQuote.data_type === 'daily_close' ? 'HISTORICAL DAILY CLOSE' : displayedMarketQuote.data_type === 'live_tick' ? 'LIVE WEBSOCKET TICK' : displayedMarketQuote.data_type === 'provider_snapshot' ? 'LATEST REST SNAPSHOT' : 'LATEST PROVIDER QUOTE'}</p><h3>{formatMoney(displayedMarketQuote.bar.close, displayedMarketQuote.currency)}</h3></div><span className={`freshness-badge freshness-${displayedMarketQuote.freshness_status}`}>{displayedMarketQuote.freshness_status.replaceAll('_', ' ')}</span></div>
                        <p className="market-provenance">{displayedMarketQuote.freshness_message} Source: {displayedMarketQuote.source} · Market timestamp: {new Date(displayedMarketQuote.market_timestamp).toLocaleString()} · Observation age: {Math.floor(displayedMarketQuote.market_age_seconds)}s · Retrieved: {new Date(displayedMarketQuote.retrieved_at).toLocaleString()} · {displayedMarketQuote.is_cached ? 'Cached' : 'Provider response'} · {displayedMarketQuote.is_synthetic ? 'Synthetic' : displayedMarketQuote.is_verified ? 'Verified mapping' : 'Unverified mapping'}</p>
                        <p className="market-provenance">Provider configured: {displayedMarketQuote.provider_configured ? 'yes' : 'no'} · Delivery: {displayedMarketQuote.delivery_mode.replaceAll('_', ' ')} · Latest request: {displayedMarketQuote.provider_request_status.replaceAll('_', ' ')} · Persistent connection: {displayedMarketQuote.provider_connection_status.replaceAll('_', ' ')} · Stream active: {displayedMarketQuote.feed_active ? 'yes' : 'no'} · Provider delay: unavailable.</p>
                        {displayedMarketQuote.fallback_reason && <p className="market-provenance">Fallback reason: {displayedMarketQuote.fallback_reason.replaceAll('_', ' ')}.</p>}
                        {displayedMarketQuote.provider_attempts.length > 0 && <p className="market-provenance">Provider attempts: {displayedMarketQuote.provider_attempts.map((attempt) => `${attempt.provider}: ${attempt.outcome.replaceAll('_', ' ')}${attempt.reason_category ? ` [${attempt.reason_category.replaceAll('_', ' ')}]` : attempt.reason ? ` (${attempt.reason.replaceAll('_', ' ')})` : ''}${attempt.latency_ms !== null && attempt.latency_ms !== undefined ? `, ${Math.round(attempt.latency_ms)} ms` : ''}`).join(' · ')}</p>}
                      </div>}
                      <div className="market-range">
                          <label>Interval<select value={marketInterval} onChange={(event) => changeMarketInterval(event.target.value as typeof marketInterval)}><option value="1m">1 minute</option><option value="5m">5 minutes</option><option value="15m">15 minutes</option><option value="1h">60 minutes</option><option value="1d">1 day</option></select></label>
                          <label>From<input type="date" value={marketStart} onChange={(event) => { setMarketStart(event.target.value); setMarketHistory(null); }} required /></label>
                          <label>Through<input type="date" value={marketEnd} onChange={(event) => { setMarketEnd(event.target.value); setMarketHistory(null); }} required /></label>
                      </div>
                      <button className="primary-button" type="button" onClick={() => void loadMarketHistory()} disabled={loadingMarketHistory}>{loadingMarketHistory ? 'Loading history…' : 'Load history'}</button>
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
                    <CandleChart bars={marketHistory.bars} tickSize={selectedInstrument.tick_size} currency={selectedInstrument.currency} interval={marketInterval} preferredSource={marketIndicators?.source} indicatorSeries={marketIndicators?.indicator_series} indicatorBarsAvailable={marketIndicators?.bars_available} onLoadOlder={loadOlderMarketHistory} loadingOlder={loadingOlderMarketHistory} hasOlder={hasOlderMarketHistory} />
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
