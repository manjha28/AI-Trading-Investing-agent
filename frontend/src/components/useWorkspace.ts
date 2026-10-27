import { useEffect, useRef, useState, type FormEvent } from 'react';
import { ApiRequestError, apiRequest, downloadCsv } from '../api';

type User = {
  id: string;
  email: string;
  created_at: string;
  email_verified: boolean;
  verification_email_sent?: boolean | null;
};
type TwoFactorStatus = {
  enabled: boolean;
  session_verified: boolean;
  setup_required: boolean;
};
type Portfolio = { id: string; name: string; base_currency: string; created_at: string };
type Position = {
  symbol: string;
  quantity: string;
  cost_basis: string;
  average_cost: string;
  holding_period_start: string | null;
};
type PortfolioSummary = {
  currency: string;
  cash_balance: string;
  invested_cost: string;
  realized_pnl: string;
  unrealized_pnl: null;
  market_value: null;
  positions: Position[];
};
type ValuationPosition = {
  symbol: string;
  instrument_id: string | null;
  quantity: string;
  cost_basis: string;
  last_price: string | null;
  market_value: string | null;
  unrealized_pnl: string | null;
  weight_percent: string | null;
  day_change: string | null;
  day_change_percent: string | null;
  freshness: 'fresh' | 'stale' | 'unavailable';
  is_synthetic: boolean;
  source: string | null;
  retrieved_at: string | null;
  message: string;
};
type PortfolioValuation = {
  currency: string;
  cash_balance: string;
  market_value: string;
  total_value: string;
  unrealized_pnl: string;
  day_change: string;
  confidence: 'reliable' | 'unreliable';
  reasons: string[];
  last_updated: string | null;
  positions: ValuationPosition[];
};
type WatchlistEntry = {
  instrument_id: string;
  symbol: string;
  display_name: string;
  asset_class: string;
  venue: string;
  currency: string;
  timezone: string;
  is_synthetic: boolean;
  relink_required: boolean;
  quote: {
    source: string;
    retrieved_at: string;
    freshness: 'fresh' | 'stale';
    freshness_status: 'fresh' | 'stale' | 'historical' | 'eod_close' | 'unavailable' | 'synthetic' | 'unverified' | 'future_dated';
    is_synthetic: boolean;
    is_verified: boolean;
    market_timestamp: string;
    market_age_seconds: number;
    is_current_session: boolean;
    absolute_change: string | null;
    percent_change: string | null;
    data_type: 'provider_quote' | 'provider_snapshot' | 'live_tick' | 'daily_close';
    close: string;
  } | null;
};
type ThesisCoverage = {
  managed: { symbol: string; instrument_id: string | null; review_overdue: boolean }[];
  unmanaged: {
    symbol: string;
    quantity: string;
    instrument_id: string | null;
    state: 'unmanaged position';
    reason: 'no_instrument_linked' | 'no_thesis' | 'thesis_not_active';
    thesis_id: string | null;
    thesis_status: string | null;
    conviction: number | null;
    review_overdue: boolean;
  }[];
};
type Transaction = {
  id: string;
  transaction_type: string;
  symbol: string | null;
  quantity: string | null;
  unit_price: string | null;
  amount: string;
  fee: string;
  occurred_at: string;
};
export type TransactionType = 'deposit' | 'withdrawal' | 'dividend' | 'buy' | 'sell';
type CsvImportRow = {
  row_number: number;
  status: 'valid' | 'duplicate' | 'invalid';
  transaction_type: string | null;
  symbol: string | null;
  errors: string[];
};
type CsvImportPreview = {
  rows: CsvImportRow[];
  valid_count: number;
  duplicate_count: number;
  invalid_count: number;
};
type JournalTrade = {
  id: string;
  asset_class: string;
  instrument: string;
  currency: string;
  side: string;
  quantity: string;
  multiplier: string;
  entry_price: string;
  exit_price: string | null;
  entry_time: string;
  exit_time: string | null;
  fees: string;
  strategy: string | null;
  setup?: string | null;
  notes?: string | null;
  tags: string[];
  planned_risk?: string | null;
  option_type?: string | null;
  strike?: string | null;
  expiry?: string | null;
  net_pnl: string | null;
  source: 'manual' | 'dhan' | 'dhan_csv';
  source_side: string | null;
  calendar_date: string;
};
type JournalBreakdown = {
  key: string;
  closed_trades: number;
  wins: number;
  losses: number;
  net_pnl: string;
  expectancy: string | null;
  profit_factor: string | null;
};
type JournalAnalytics = {
  currency: string;
  closed_trades: number;
  open_trades: number;
  trades_logged: number;
  wins: number;
  losses: number;
  unpriced_closed_trades: number;
  win_rate: string | null;
  net_pnl: string | null;
  expectancy: string | null;
  profit_factor: string | null;
  max_drawdown: string | null;
  daily: {
    date: string;
    trade_count: number;
    realized_pnl: string | null;
    complete: boolean;
  }[];
};
type MarketInstrument = {
  id: string;
  symbol: string;
  display_name: string;
  asset_class: string;
  venue: string;
  currency: string;
  country: string;
  timezone: string;
  multiplier: string | null;
  tick_size: string | null;
  expiry: string | null;
  strike: string | null;
  option_type: string | null;
  is_synthetic: boolean;
  relink_required: boolean;
  provider_identifiers: { provider: string; provider_instrument_id: string; provider_symbol: string; is_verified: boolean }[];
};
type MarketBar = {
  timestamp: string;
  open: string;
  high: string;
  low: string;
  close: string;
  volume: string;
  source: string;
  retrieved_at: string;
  is_synthetic: boolean;
};
type MarketInterval = '1d' | '1h' | '15m' | '5m' | '1m';
type MarketHistory = {
  instrument_id: string;
  interval: string;
  provider_attempts: {
    provider: string;
    outcome: 'used' | 'unavailable' | 'unsupported' | 'stale' | 'skipped' | 'rate_limited' | 'no_new_bars' | 'config_blocker';
    reason: string | null;
    reason_category: string | null;
    http_status: number | null;
    latency_ms: number | null;
    bars: number;
  }[];
  fallback_reason: string | null;
  provider: string | null;
  fetched_at: string | null;
  as_of: string | null;
  currency: string | null;
  session_state: 'open' | 'closed' | 'unknown';
  is_cached: boolean;
  sync_status: 'success' | 'unavailable' | 'no_new_bars';
  freshness: {
    status: 'fresh' | 'stale' | 'unavailable' | 'never_fetched';
    source: string | null;
    last_successful_update: string | null;
    latest_bar_timestamp: string | null;
    is_synthetic: boolean;
    message: string;
  };
  bars: MarketBar[];
};
type IndicatorValue = {
  value: string | null;
  is_synthetic: boolean;
  bars_required: number;
  bars_used: number;
  status: 'available' | 'insufficient_history';
};
type InstrumentIndicators = {
  instrument_id: string;
  symbol: string;
  currency: string;
  interval: string;
  freshness: {
    status: 'fresh' | 'stale' | 'unavailable' | 'never_fetched';
    source: string | null;
    is_synthetic: boolean;
    message: string;
  };
  source: string | null;
  latest_bar: string | null;
  bars_available: number;
  indicators: Record<string, IndicatorValue>;
  indicator_series: Record<string, { timestamp: string; value: string }[]>;
  disclaimer: string;
};

export const indicatorLabels: Record<string, { label: string; unit: 'price' | 'percent' | 'index' | 'z-score' }> = {
  sma_20: { label: 'SMA · 20', unit: 'price' },
  sma_50: { label: 'SMA · 50', unit: 'price' },
  sma_200: { label: 'SMA · 200', unit: 'price' },
  ema_20: { label: 'EMA · 20', unit: 'price' },
  ema_50: { label: 'EMA · 50', unit: 'price' },
  ema_200: { label: 'EMA · 200', unit: 'price' },
  rsi_14: { label: 'RSI · 14', unit: 'index' },
  atr_14: { label: 'ATR · 14', unit: 'price' },
  realized_volatility_20d: { label: 'Realized volatility · 20d', unit: 'percent' },
  drawdown_from_20d_peak: { label: 'Drawdown from 20-bar peak', unit: 'percent' },
  distance_to_52w_high: { label: 'Distance to 52-week high', unit: 'percent' },
  distance_to_52w_low: { label: 'Distance to 52-week low', unit: 'percent' },
  volume_z_score_20d: { label: 'Volume z-score · 20d', unit: 'z-score' },
  gap_percent: { label: 'Opening gap', unit: 'percent' },
};
type RiskMetrics = {
  portfolio_id: string;
  as_of: string;
  currency: string;
  position_weights: {
    symbol: string;
    weight_pct: string | null;
    valuation_unavailable_reason: string | null;
  }[];
  top_three_concentration_pct: string | null;
  hhi: string | null;
  cash_balance: string;
  cash_pct: string | null;
  currency_exposure: {
    currency: string;
    position_count: number;
    market_value: string | null;
    valued_positions: number;
    synthetic_positions: number;
    stale_positions: number;
  }[];
  ltcg_boundary_positions: { symbol: string; ltcg_boundary_lots: { days_held: number; quantity: string }[] }[];
  tax_disclaimer: string;
  concentration_unavailable_reason: string | null;
  disclaimer: string;
};
type MarketQuote = {
  instrument_id: string;
  symbol: string;
  currency: string;
  source: string;
  retrieved_at: string;
  freshness: 'fresh' | 'stale';
  freshness_status: 'fresh' | 'stale' | 'historical' | 'eod_close' | 'unavailable' | 'synthetic' | 'unverified' | 'future_dated';
  freshness_message: string;
  is_current_session: boolean;
  is_cached: boolean;
  is_synthetic: boolean;
  is_verified: boolean;
  provider_configured: boolean;
  provider_request_status: 'succeeded' | 'not_attempted' | 'failed';
  provider_attempts: {
    provider: string;
    outcome: 'used' | 'unavailable' | 'unsupported' | 'stale' | 'skipped' | 'rate_limited' | 'no_new_bars' | 'config_blocker';
    reason: string | null;
    reason_category: string | null;
    http_status: number | null;
    latency_ms: number | null;
    bars: number;
  }[];
  fallback_reason: string | null;
  provider_connection_status: 'not_applicable_rest' | 'connected' | 'reconnecting' | 'disconnected' | 'unavailable';
  feed_active: boolean;
  market_timestamp: string;
  market_age_seconds: number;
  absolute_change: string | null;
  percent_change: string | null;
  data_type: 'provider_quote' | 'provider_snapshot' | 'live_tick' | 'daily_close';
  delivery_mode: 'on_demand_rest' | 'rest_snapshot' | 'websocket_tick';
  stream_status: 'not_configured' | 'active' | 'reconnecting' | 'stale' | 'unavailable';
  provider_delay_seconds: null;
  provider: string | null;
  fetched_at: string | null;
  as_of: string | null;
  interval: string;
  session_state: 'open' | 'closed' | 'unknown';
  bar: {
    timestamp: string;
    open: string;
    high: string;
    low: string;
    close: string;
    volume: string;
  };
};

export function formatMoney(value: string, currency: string): string {
  if (currency.length !== 3) {
    return `${new Intl.NumberFormat('en-US', {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    }).format(Number(value))} ${currency}`;
  }
  return new Intl.NumberFormat(currency === 'INR' ? 'en-IN' : 'en-US', {
    style: 'currency',
    currency,
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(Number(value));
}

export function formatQuantity(value: string): string {
  return new Intl.NumberFormat('en-US', { maximumFractionDigits: 8 }).format(Number(value));
}

export function localDateTime(value: string | null | undefined): string {
  if (!value) return '';
  const date = new Date(value);
  return new Date(date.getTime() - date.getTimezoneOffset() * 60_000).toISOString().slice(0, 16);
}

function isoDateInput(date: Date): string {
  return date.toISOString().slice(0, 10);
}

export function marketRangeParams(interval: string, startDate: string, endDate: string): URLSearchParams {
  const start = new Date(`${startDate}T00:00:00Z`);
  const endExclusive = new Date(`${endDate}T00:00:00Z`);
  if (!startDate || !endDate || Number.isNaN(start.getTime()) || Number.isNaN(endExclusive.getTime()) || start > endExclusive) {
    throw new Error('Choose a valid date range.');
  }
  endExclusive.setUTCDate(endExclusive.getUTCDate() + 1);
  return new URLSearchParams({
    interval,
    start: start.toISOString(),
    end: endExclusive.toISOString(),
    limit: '500',
  });
}

function readEmailVerificationToken(): string | null {
  const match = window.location.hash.match(/^#verify-email\/([A-Za-z0-9_-]{32,128})$/);
  if (!match) return null;
  window.history.replaceState(null, '', `${window.location.pathname}${window.location.search}`);
  return match[1];
}

export function useWorkspace() {
  const [user, setUser] = useState<User | null>(null);
  const [checkingSession, setCheckingSession] = useState(true);
  const [verificationToken] = useState(readEmailVerificationToken);
  const [verificationStatus, setVerificationStatus] = useState<
    'verifying' | 'verified' | 'expired' | 'invalid' | 'reused' | 'failed'
  >('verifying');
  const verificationStarted = useRef(false);
  const [twoFactorStatus, setTwoFactorStatus] = useState<TwoFactorStatus | null>(null);
  const [twoFactorSecret, setTwoFactorSecret] = useState('');
  const [twoFactorProvisioningUri, setTwoFactorProvisioningUri] = useState('');
  const [twoFactorCode, setTwoFactorCode] = useState('');
  const [twoFactorBusy, setTwoFactorBusy] = useState(false);
  const [twoFactorError, setTwoFactorError] = useState('');
  const [onboardingDismissed, setOnboardingDismissed] = useState(false);
  const [registering, setRegistering] = useState(false);
  const [portfolios, setPortfolios] = useState<Portfolio[]>([]);
  const [portfolioId, setPortfolioId] = useState('');
  const [summary, setSummary] = useState<PortfolioSummary | null>(null);
  const [valuation, setValuation] = useState<PortfolioValuation | null>(null);
  const [riskMetrics, setRiskMetrics] = useState<RiskMetrics | null>(null);
  const [watchlist, setWatchlist] = useState<WatchlistEntry[]>([]);
  const [coverage, setCoverage] = useState<ThesisCoverage | null>(null);
  const [thesisSymbol, setThesisSymbol] = useState<string | null>(null);
  const [coverageVersion, setCoverageVersion] = useState(0);
  const [refreshingValuation, setRefreshingValuation] = useState(false);
  const [transactions, setTransactions] = useState<Transaction[]>([]);
  const [editingTransaction, setEditingTransaction] = useState<Transaction | null>(null);
  const [csvContent, setCsvContent] = useState('');
  const [csvPreview, setCsvPreview] = useState<CsvImportPreview | null>(null);
  const [importingCsv, setImportingCsv] = useState(false);
  const [activeView, setActiveView] = useState<'overview' | 'activity' | 'journal' | 'market-data' | 'brokers' | 'alerts' | 'corporate-actions' | 'research'>(
    () => new URLSearchParams(window.location.search).has('broker_connected') ? 'brokers' : 'overview',
  );
  const [openMarketInstrumentId, setOpenMarketInstrumentId] = useState<string | null>(null);
  useEffect(() => {
    const url = new URL(window.location.href);
    if (!url.searchParams.has('broker_connected')) return;
    url.searchParams.delete('broker_connected');
    window.history.replaceState(
      window.history.state,
      '',
      `${url.pathname}${url.search}${url.hash}`,
    );
  }, []);
  const [transactionType, setTransactionType] = useState<TransactionType>('deposit');
  const [journalTrades, setJournalTrades] = useState<JournalTrade[]>([]);
  const [journalAnalytics, setJournalAnalytics] = useState<JournalAnalytics | null>(null);
  const [journalBreakdown, setJournalBreakdown] = useState<JournalBreakdown[]>([]);
  const [editingTrade, setEditingTrade] = useState<JournalTrade | null>(null);
  const [marketQuery, setMarketQuery] = useState('');
  const [marketInstruments, setMarketInstruments] = useState<MarketInstrument[]>([]);
  const [selectedInstrument, setSelectedInstrument] = useState<MarketInstrument | null>(null);
  const [marketHistory, setMarketHistory] = useState<MarketHistory | null>(null);
  const [loadingOlderMarketHistory, setLoadingOlderMarketHistory] = useState(false);
  const [hasOlderMarketHistory, setHasOlderMarketHistory] = useState(true);
  const olderHistoryRequest = useRef(false);
  const recentMarketReads = useRef(new Map<string, number>());
  const [marketIndicators, setMarketIndicators] = useState<InstrumentIndicators | null>(null);
  const [marketQuote, setMarketQuote] = useState<MarketQuote | null>(null);
  const [marketInterval, setMarketInterval] = useState<MarketInterval>('1d');
  const [marketStart, setMarketStart] = useState(
    isoDateInput(new Date(Date.now() - 14 * 24 * 60 * 60 * 1000)),
  );
  const [marketEnd, setMarketEnd] = useState(isoDateInput(new Date()));
  const [searchingMarket, setSearchingMarket] = useState(false);
  const [loadingMarketHistory, setLoadingMarketHistory] = useState(false);
  const [loadingMarketIndicators, setLoadingMarketIndicators] = useState(false);
  const [loadingMarketQuote, setLoadingMarketQuote] = useState(false);
  const [journalAssetClass, setJournalAssetClass] = useState('equity');
  const [journalCurrency, setJournalCurrency] = useState('INR');
  const [journalHasExit, setJournalHasExit] = useState(false);
  const [loadingPortfolio, setLoadingPortfolio] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');

  function skipRecentMarketRead(key: string, force = false): boolean {
    const now = Date.now();
    const previous = recentMarketReads.current.get(key);
    if (!force && previous !== undefined && now - previous < 3000) return true;
    recentMarketReads.current.set(key, now);
    for (const [requestKey, requestedAt] of recentMarketReads.current) {
      if (now - requestedAt >= 10_000) recentMarketReads.current.delete(requestKey);
    }
    return false;
  }

  useEffect(() => {
    if (!verificationToken || verificationStarted.current) return;
    verificationStarted.current = true;
    apiRequest<User>('/auth/verify-email', {
      method: 'POST',
      body: JSON.stringify({ token: verificationToken }),
    })
      .then((verifiedUser) => {
        setUser((current) => current?.id === verifiedUser.id ? verifiedUser : current);
        setVerificationStatus('verified');
      })
      .catch((requestError: Error) => {
        if (!(requestError instanceof ApiRequestError)) {
          setVerificationStatus('failed');
        } else if (requestError.status === 410) {
          setVerificationStatus('expired');
        } else if (requestError.code === 'verification_token_reused') {
          setVerificationStatus('reused');
        } else if (
          requestError.code === 'verification_token_invalid'
          || requestError.status === 422
        ) {
          setVerificationStatus('invalid');
        } else {
          setVerificationStatus('failed');
        }
      });
  }, [verificationToken]);

  useEffect(() => {
    apiRequest<User>('/auth/me')
      .then(setUser)
      .catch(() => setUser(null))
      .finally(() => setCheckingSession(false));
  }, []);

  useEffect(() => {
    if (!user) {
      setTwoFactorStatus(null);
      return;
    }
    let active = true;
    apiRequest<TwoFactorStatus>('/auth/2fa/status')
      .then((status) => {
        if (active) setTwoFactorStatus(status);
      })
      .catch((requestError: Error) => {
        if (active) setTwoFactorError(requestError.message);
      });
    return () => {
      active = false;
    };
  }, [user]);

  useEffect(() => {
    if (!user) return;
    apiRequest<Portfolio[]>('/portfolios')
      .then((items) => {
        setPortfolios(items);
        setPortfolioId((current) => current || items[0]?.id || '');
      })
      .catch((requestError: Error) => setError(requestError.message));
  }, [user]);

  useEffect(() => {
    if (!user) {
      setWatchlist([]);
      return;
    }
    let active = true;
    apiRequest<WatchlistEntry[]>('/market-data/watchlist')
      .then((items) => {
        if (active) setWatchlist(items);
      })
      .catch((requestError: Error) => {
        if (active) setError(requestError.message);
      });
    return () => {
      active = false;
    };
  }, [user]);

  useEffect(() => {
    if (!portfolioId) {
      setSummary(null);
      setTransactions([]);
      return;
    }
    let active = true;
    setLoadingPortfolio(true);
    Promise.all([
      apiRequest<PortfolioSummary>(`/portfolios/${portfolioId}/summary`),
      apiRequest<Transaction[]>(`/portfolios/${portfolioId}/transactions`),
    ])
      .then(([nextSummary, nextTransactions]) => {
        if (!active) return;
        setSummary(nextSummary);
        setTransactions(nextTransactions);
      })
      .catch((requestError: Error) => {
        if (active) setError(requestError.message);
      })
      .finally(() => {
        if (active) setLoadingPortfolio(false);
      });
    return () => {
      active = false;
    };
  }, [portfolioId]);

  useEffect(() => {
    if (!portfolioId) {
    setValuation(null);
    return;
    }
    let active = true;
    setRefreshingValuation(true);
    apiRequest<PortfolioValuation>(`/portfolios/${portfolioId}/valuation`)
    .then((nextValuation) => {
      if (active) setValuation(nextValuation);
    })
    .catch((requestError: Error) => {
      if (active) setError(requestError.message);
    })
    .finally(() => {
      if (active) setRefreshingValuation(false);
    });
    return () => {
    active = false;
    };
  }, [portfolioId, transactions]);

  useEffect(() => {
    if (!portfolioId) {
      setRiskMetrics(null);
      return;
    }
    let active = true;
    setRiskMetrics(null);
    apiRequest<RiskMetrics>(`/portfolios/${portfolioId}/risk-metrics`)
      .then((metrics) => {
        if (active) setRiskMetrics(metrics);
      })
      .catch((requestError: Error) => {
        if (active) setError(requestError.message);
      });
    return () => {
      active = false;
    };
  }, [portfolioId, transactions, valuation]);

  useEffect(() => {
    if (!portfolioId) {
      setCoverage(null);
      return;
    }
    let active = true;
    apiRequest<ThesisCoverage>(`/portfolios/${portfolioId}/theses/coverage`)
      .then((next) => {
        if (active) setCoverage(next);
      })
      .catch(() => {
        if (active) setCoverage(null);
      });
    return () => {
      active = false;
    };
  }, [portfolioId, transactions, coverageVersion]);

  useEffect(() => {
    setEditingTransaction(null);
    setCsvContent('');
    setCsvPreview(null);
  }, [portfolioId]);

  useEffect(() => {
    if (!user || activeView !== 'journal') return;
    let active = true;
    refreshJournalTrades()
      .then(() => {
        if (!active) return;
      })
      .catch((requestError: Error) => {
        if (active) setError(requestError.message);
      });
    return () => {
      active = false;
    };
  }, [activeView, journalCurrency, user]);

  async function refreshJournalTrades() {
    const [trades, analytics, breakdown] = await Promise.all([
      apiRequest<JournalTrade[]>(`/trades/journal?currency=${journalCurrency}`),
      apiRequest<JournalAnalytics>(`/trades/analytics/journal?currency=${journalCurrency}`),
      apiRequest<JournalBreakdown[]>(`/trades/analytics/breakdown?currency=${journalCurrency}&group_by=strategy`),
    ]);
    setJournalTrades(trades);
    setJournalAnalytics(analytics);
    setJournalBreakdown(breakdown);
  }

  async function submitAuth(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError('');
    const form = new FormData(event.currentTarget);
    try {
      if (registering) {
        const result = await apiRequest<{ message: string }>('/auth/register', {
          method: 'POST',
          body: JSON.stringify({ email: form.get('email'), password: form.get('password') }),
        });
        setRegistering(false);
        setNotice(`${result.message} Check your inbox and spam folder. After verifying, sign in to continue.`);
        return;
      }
      const result = await apiRequest<User>('/auth/login', {
        method: 'POST',
        body: JSON.stringify({ email: form.get('email'), password: form.get('password') }),
      });
      setUser(result);
      if (!result.email_verified) {
        setNotice('Verify your email to unlock broker connections. You can continue using manual portfolio features.');
      } else {
        setNotice('Sign-in successful. Verify or set up app-based two-factor authentication to access brokers.');
      }
    } catch (requestError) {
      setError((requestError as Error).message);
    }
  }

  async function createPortfolio(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError('');
    const formElement = event.currentTarget;
    const form = new FormData(formElement);
    try {
      const portfolio = await apiRequest<Portfolio>('/portfolios', {
        method: 'POST',
        body: JSON.stringify({ name: form.get('name'), base_currency: form.get('base_currency') }),
      });
      setPortfolios((current) => [...current, portfolio]);
      setPortfolioId(portfolio.id);
      formElement.reset();
    } catch (requestError) {
      setError((requestError as Error).message);
    }
  }

  async function submitTransaction(event: FormEvent<HTMLFormElement>): Promise<boolean> {
    event.preventDefault();
    if (!portfolioId) return false;
    setError('');
    const formElement = event.currentTarget;
    const form = new FormData(formElement);
    const payload: Record<string, string> = {
      transaction_type: transactionType,
      fee: String(form.get('fee') || '0.00'),
      occurred_at: new Date(String(form.get('occurred_at'))).toISOString(),
    };
    if (['buy', 'sell'].includes(transactionType)) {
      payload.symbol = String(form.get('symbol'));
      payload.quantity = String(form.get('quantity'));
      payload.unit_price = String(form.get('unit_price'));
    } else {
      payload.amount = String(form.get('amount'));
      if (transactionType === 'dividend') payload.symbol = String(form.get('symbol'));
    }

    try {
      await apiRequest<Transaction>(
        `/portfolios/${portfolioId}/transactions${editingTransaction ? `/${editingTransaction.id}` : ''}`,
        {
        method: editingTransaction ? 'PUT' : 'POST',
        body: JSON.stringify(payload),
        },
      );
      const [nextSummary, nextTransactions] = await Promise.all([
        apiRequest<PortfolioSummary>(`/portfolios/${portfolioId}/summary`),
        apiRequest<Transaction[]>(`/portfolios/${portfolioId}/transactions`),
      ]);
      setSummary(nextSummary);
      setTransactions(nextTransactions);
      setEditingTransaction(null);
      formElement.reset();
      setTransactionType('deposit');
      return true;
    } catch (requestError) {
      setError((requestError as Error).message);
      return false;
    }
  }

  async function loadTransactionCsv(file: File | undefined) {
    setError('');
    setCsvPreview(null);
    if (!file) {
      setCsvContent('');
      return;
    }
    if (file.size > 1_000_000) {
      setCsvContent('');
      setError('CSV files must be 1 MB or smaller.');
      return;
    }
    try {
      setCsvContent(await file.text());
    } catch {
      setCsvContent('');
      setError('The selected CSV file could not be read.');
    }
  }

  async function previewTransactionCsv() {
    if (!portfolioId || !csvContent) return;
    setError('');
    setCsvPreview(null);
    try {
      const preview = await apiRequest<CsvImportPreview>(
        `/portfolios/${portfolioId}/transactions/import/preview`,
        { method: 'POST', body: JSON.stringify({ csv_content: csvContent }) },
      );
      setCsvPreview(preview);
    } catch (requestError) {
      setError((requestError as Error).message);
    }
  }

  async function importTransactionCsv() {
    if (!portfolioId || !csvContent || !csvPreview || csvPreview.invalid_count > 0) return;
    setError('');
    setNotice('');
    setImportingCsv(true);
    try {
      const result = await apiRequest<{ imported_count: number; skipped_duplicate_count: number }>(
        `/portfolios/${portfolioId}/transactions/import`,
        { method: 'POST', body: JSON.stringify({ csv_content: csvContent }) },
      );
      const [nextSummary, nextTransactions] = await Promise.all([
        apiRequest<PortfolioSummary>(`/portfolios/${portfolioId}/summary`),
        apiRequest<Transaction[]>(`/portfolios/${portfolioId}/transactions`),
      ]);
      setSummary(nextSummary);
      setTransactions(nextTransactions);
      setCsvContent('');
      setCsvPreview(null);
      setNotice(
        `Imported ${result.imported_count} transaction${result.imported_count === 1 ? '' : 's'}; skipped ${result.skipped_duplicate_count} duplicate${result.skipped_duplicate_count === 1 ? '' : 's'}.`,
      );
    } catch (requestError) {
      setError((requestError as Error).message);
    } finally {
      setImportingCsv(false);
    }
  }

  async function exportTransactionCsv() {
    if (!portfolioId) return;
    setError('');
    try {
      await downloadCsv(
        `/portfolios/${portfolioId}/transactions/export`,
        `portfolio-${portfolioId}-transactions.csv`,
      );
    } catch (requestError) {
      setError((requestError as Error).message);
    }
  }

  async function submitJournalTrade(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError('');
    const formElement = event.currentTarget;
    const form = new FormData(formElement);
    const toIso = (key: string) => new Date(String(form.get(key))).toISOString();
    const payload: Record<string, string | string[] | null> = {
      asset_class: journalAssetClass,
      currency: String(form.get('currency')),
      instrument: String(form.get('instrument')),
      side: String(form.get('side')),
      quantity: String(form.get('quantity')),
      multiplier: String(form.get('multiplier')),
      entry_price: String(form.get('entry_price')),
      entry_time: toIso('entry_time'),
      fees: String(form.get('fees') || '0.00'),
      tags: String(form.get('tags') || '').split(',').map((tag) => tag.trim()).filter(Boolean),
      strategy: String(form.get('strategy') || '').trim() || null,
      setup: String(form.get('setup') || '').trim() || null,
      notes: String(form.get('notes') || '').trim() || null,
      planned_risk: String(form.get('planned_risk') || '').trim() || null,
    };
    if (journalHasExit) {
      payload.exit_price = String(form.get('exit_price'));
      payload.exit_time = toIso('exit_time');
    }
    if (journalAssetClass === 'option') {
      payload.option_type = String(form.get('option_type'));
      payload.strike = String(form.get('strike'));
      payload.expiry = new Date(String(form.get('expiry'))).toISOString();
    }

    try {
      await apiRequest<JournalTrade>(editingTrade ? `/trades/${editingTrade.id}` : '/trades', {
        method: editingTrade ? 'PUT' : 'POST',
        body: JSON.stringify(payload),
      });
      await refreshJournalTrades();
      setEditingTrade(null);
      setJournalHasExit(false);
      setJournalAssetClass('equity');
      formElement.reset();
    } catch (requestError) {
      setError((requestError as Error).message);
    }
  }

  async function removeJournalTrade(tradeId: string) {
    if (!window.confirm('Delete this journal trade?')) return;
    setError('');
    try {
      await apiRequest<void>(`/trades/${tradeId}`, { method: 'DELETE' });
      await refreshJournalTrades();
    } catch (requestError) {
      setError((requestError as Error).message);
    }
  }

  async function removeTransaction(transactionId: string) {
    if (!portfolioId) return;
    if (!window.confirm('Delete this transaction? The portfolio ledger will be recalculated.')) return;
    setError('');
    try {
      await apiRequest<void>(`/portfolios/${portfolioId}/transactions/${transactionId}`, { method: 'DELETE' });
      const [nextSummary, nextTransactions] = await Promise.all([
        apiRequest<PortfolioSummary>(`/portfolios/${portfolioId}/summary`),
        apiRequest<Transaction[]>(`/portfolios/${portfolioId}/transactions`),
      ]);
      setSummary(nextSummary);
      setTransactions(nextTransactions);
    } catch (requestError) {
      setError((requestError as Error).message);
    }
  }

  function startEditingTransaction(transaction: Transaction) {
    setEditingTransaction(transaction);
    setTransactionType(transaction.transaction_type as TransactionType);
    setError('');
  }

  function cancelEditingTransaction() {
    setEditingTransaction(null);
    setTransactionType('deposit');
  }

  function startEditingTrade(trade: JournalTrade) {
    setEditingTrade(trade);
    setJournalAssetClass(trade.asset_class);
    setJournalHasExit(trade.exit_price !== null);
    setError('');
  }

  function cancelEditingTrade() {
    setEditingTrade(null);
    setJournalAssetClass('equity');
    setJournalHasExit(false);
  }

  async function searchMarketInstruments(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError('');
    setNotice('');
    setMarketInstruments([]);
    setSelectedInstrument(null);
    setMarketHistory(null);
    setMarketQuote(null);
    setSearchingMarket(true);
    try {
      const query = new URLSearchParams({ query: marketQuery.trim(), limit: '30' });
      const instruments = await apiRequest<MarketInstrument[]>(`/market-data/instruments?${query}`);
      setMarketInstruments(instruments);
      if (instruments.length === 0) setNotice('No instruments matched this search.');
    } catch (requestError) {
      setError((requestError as Error).message);
    } finally {
      setSearchingMarket(false);
    }
  }

  async function selectMarketInstrument(instrument: Pick<MarketInstrument, 'id'>) {
    const requestKey = `select:${instrument.id}:${marketInterval}:${marketStart}:${marketEnd}`;
    if (skipRecentMarketRead(requestKey)) return;
    setError('');
    setMarketHistory(null);
    setHasOlderMarketHistory(true);
    setMarketIndicators(null);
    setMarketQuote(null);
    try {
      const params = marketRangeParams(marketInterval, marketStart, marketEnd);
      const [details, history] = await Promise.all([
        apiRequest<MarketInstrument>(`/market-data/instruments/${instrument.id}`),
        apiRequest<MarketHistory>(
          `/market-data/instruments/${instrument.id}/history?${params}`,
        ),
      ]);
      setSelectedInstrument(details);
      setMarketHistory(history);
      void loadMarketQuote(details);
      void loadInstrumentIndicators(details, marketInterval, history.provider);
    } catch (requestError) {
      setError((requestError as Error).message);
    }
  }

  async function loadInstrumentIndicators(
    instrument: MarketInstrument,
    interval = marketInterval,
    source?: string | null,
  ) {
    if (skipRecentMarketRead(`indicators:${instrument.id}:${interval}:${source ?? ''}`)) return;
    setMarketIndicators(null);
    setLoadingMarketIndicators(true);
    try {
      const query = new URLSearchParams({ interval });
      if (source) query.set('source', source);
      const indicators = await apiRequest<InstrumentIndicators>(
        `/instruments/${instrument.id}/indicators?${query}`,
      );
      setMarketIndicators(indicators);
    } catch (requestError) {
      setError((requestError as Error).message);
    } finally {
      setLoadingMarketIndicators(false);
    }
  }

  async function loadMarketQuote(instrument = selectedInstrument, force = false) {
    if (!instrument) return;
    if (skipRecentMarketRead(`quote:${instrument.id}`, force)) return;
    setError('');
    setMarketQuote(null);
    setLoadingMarketQuote(true);
    try {
      const quote = await apiRequest<MarketQuote>(
        `/market-data/instruments/${instrument.id}/quote${force ? '?refresh=true' : ''}`,
      );
      setMarketQuote(quote);
      setWatchlist((current) => current.some((item) => item.instrument_id === instrument.id)
        ? current.map((item) => (
        item.instrument_id === instrument.id
          ? {
            ...item,
            quote: {
              source: quote.source,
              retrieved_at: quote.retrieved_at,
              freshness: quote.freshness,
              freshness_status: quote.freshness_status,
              is_synthetic: quote.is_synthetic,
              is_verified: quote.is_verified,
              market_timestamp: quote.market_timestamp,
              market_age_seconds: quote.market_age_seconds,
              is_current_session: quote.is_current_session,
              absolute_change: quote.absolute_change,
              percent_change: quote.percent_change,
              data_type: quote.data_type,
              close: quote.bar.close,
            },
          }
          : item
        ))
        : current);
    } catch (requestError) {
      setError((requestError as Error).message);
    } finally {
      setLoadingMarketQuote(false);
    }
  }

  async function refreshPortfolioValuation() {
    if (!portfolioId) return;
    setError('');
    setRefreshingValuation(true);
    try {
      const nextValuation = await apiRequest<PortfolioValuation>(
        `/portfolios/${portfolioId}/valuation?refresh=true`,
      );
      setValuation(nextValuation);
      if (watchlist.length > 0) await refreshWatchlist(true);
    } catch (requestError) {
      setError((requestError as Error).message);
    } finally {
      setRefreshingValuation(false);
    }
  }

  async function attachSelectedInstrument() {
    if (!portfolioId || !selectedInstrument) return;
    try {
      const result = await apiRequest<{ linked_transactions: number }>(
        `/portfolios/${portfolioId}/positions/${encodeURIComponent(selectedInstrument.symbol)}/instrument`,
        {
          method: 'POST',
          body: JSON.stringify({ instrument_id: selectedInstrument.id }),
        },
      );
      setNotice(
        `Linked ${selectedInstrument.symbol} to ${result.linked_transactions} transaction${result.linked_transactions === 1 ? '' : 's'}.`,
      );
      await refreshPortfolioValuation();
    } catch (requestError) {
      setError((requestError as Error).message);
    }
  }

  async function addSelectedInstrumentToWatchlist() {
    if (!selectedInstrument) return;
    await addInstrumentToWatchlist(selectedInstrument.id, selectedInstrument.symbol);
  }

  async function addInstrumentToWatchlist(instrumentId: string, symbol: string) {
    try {
      await apiRequest<void>(`/market-data/watchlist/${instrumentId}`, { method: 'POST' });
      await refreshWatchlist(true);
      setNotice(`${symbol} added to the quote-refresh watchlist.`);
    } catch (requestError) {
      setError((requestError as Error).message);
    }
  }

  async function removeInstrumentFromWatchlist(instrumentId: string) {
    try {
      await apiRequest<void>(`/market-data/watchlist/${instrumentId}`, { method: 'DELETE' });
      await refreshWatchlist(true);
      setNotice('Instrument removed from the quote-refresh watchlist.');
    } catch (requestError) {
      setError((requestError as Error).message);
    }
  }

  async function refreshWatchlist(force = false) {
    if (skipRecentMarketRead('watchlist:default', force)) return;
    setWatchlist(await apiRequest<WatchlistEntry[]>('/market-data/watchlist'));
  }

  async function loadMarketHistory(
    interval = marketInterval,
    startDate = marketStart,
    endDate = marketEnd,
  ) {
    if (!selectedInstrument || !startDate || !endDate) return;
    const requestKey = `history:${selectedInstrument.id}:${interval}:${startDate}:${endDate}`;
    if (skipRecentMarketRead(requestKey)) return;
    setError('');
    setMarketHistory(null);
    setHasOlderMarketHistory(true);
    setLoadingMarketHistory(true);
    try {
      const params = marketRangeParams(interval, startDate, endDate);
      const history = await apiRequest<MarketHistory>(
        `/market-data/instruments/${selectedInstrument.id}/history/sync?${params}`,
        { method: 'POST' },
      );
      setMarketHistory(history);
      await loadInstrumentIndicators(selectedInstrument, interval, history.provider);
    } catch (requestError) {
      setError((requestError as Error).message);
      if (selectedInstrument) {
        try {
          const params = marketRangeParams(interval, startDate, endDate);
          const history = await apiRequest<MarketHistory>(
            `/market-data/instruments/${selectedInstrument.id}/history?${params}`,
          );
          setMarketHistory(history);
          await loadInstrumentIndicators(selectedInstrument, interval, history.provider);
        } catch (historyError) {
          setError(`${(requestError as Error).message}; saved history unavailable: ${(historyError as Error).message}`);
        }
      }
    } finally {
      setLoadingMarketHistory(false);
    }
  }

  async function loadOlderMarketHistory() {
    if (
      !selectedInstrument
      || !marketHistory?.bars.length
      || !hasOlderMarketHistory
      || olderHistoryRequest.current
    ) return;
    olderHistoryRequest.current = true;
    setLoadingOlderMarketHistory(true);
    try {
      const oldestTimestamp = Math.min(
        ...marketHistory.bars.map((bar) => new Date(bar.timestamp).getTime()),
      );
      const end = new Date(oldestTimestamp);
      const start = new Date(oldestTimestamp - 90 * 24 * 60 * 60 * 1000);
      const params = new URLSearchParams({
        interval: marketInterval,
        start: start.toISOString(),
        end: end.toISOString(),
        limit: '500',
      });
      const olderHistory = await apiRequest<MarketHistory>(
        `/market-data/instruments/${selectedInstrument.id}/history/sync?${params}`,
        { method: 'POST' },
      );
      const mergedBars = new Map(
        [...marketHistory.bars, ...olderHistory.bars].map((bar) => [
          `${bar.timestamp}-${bar.source}`,
          bar,
        ]),
      );
      const bars = [...mergedBars.values()].sort(
        (left, right) => new Date(left.timestamp).getTime() - new Date(right.timestamp).getTime(),
      );
      setMarketHistory({ ...olderHistory, bars });
      await loadInstrumentIndicators(selectedInstrument, marketInterval, olderHistory.provider);
      const newOldestTimestamp = bars.length
        ? new Date(bars[0].timestamp).getTime()
        : oldestTimestamp;
      if (newOldestTimestamp >= oldestTimestamp) setHasOlderMarketHistory(false);
    } catch (requestError) {
      setError((requestError as Error).message);
    } finally {
      olderHistoryRequest.current = false;
      setLoadingOlderMarketHistory(false);
    }
  }

  async function signOut() {
    setError('');
    setNotice('');
    try {
      await apiRequest<void>('/auth/logout', { method: 'POST' });
      setUser(null);
      setTwoFactorStatus(null);
      setPortfolios([]);
      setPortfolioId('');
    } catch (requestError) {
      setError((requestError as Error).message);
    }
  }

  async function resendVerificationEmail() {
    setError('');
    setNotice('');
    try {
      const result = await apiRequest<{ message: string }>('/auth/verification/resend', {
        method: 'POST',
        body: '{}',
      });
      setNotice(result.message);
    } catch (requestError) {
      setError((requestError as Error).message);
    }
  }

  async function beginTwoFactorSetup() {
    setTwoFactorError('');
    setTwoFactorBusy(true);
    try {
      const result = await apiRequest<{ secret: string; provisioning_uri: string }>('/auth/2fa/setup', {
        method: 'POST',
        body: '{}',
      });
      setTwoFactorSecret(result.secret);
      setTwoFactorProvisioningUri(result.provisioning_uri);
    } catch (requestError) {
      setTwoFactorError((requestError as Error).message);
    } finally {
      setTwoFactorBusy(false);
    }
  }

  async function verifyTwoFactor(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setTwoFactorError('');
    setTwoFactorBusy(true);
    try {
      const result = await apiRequest<TwoFactorStatus>('/auth/2fa/verify', {
        method: 'POST',
        body: JSON.stringify({ code: twoFactorCode }),
      });
      setTwoFactorStatus({ ...result, setup_required: false });
      setTwoFactorSecret('');
      setTwoFactorProvisioningUri('');
      setTwoFactorCode('');
      setNotice('Two-factor authentication is verified for this session. Broker access is available.');
    } catch (requestError) {
      setTwoFactorError((requestError as Error).message);
    } finally {
      setTwoFactorBusy(false);
    }
  }


  const selectedPortfolio = portfolios.find((portfolio) => portfolio.id === portfolioId);
  const currency = summary?.currency ?? selectedPortfolio?.base_currency ?? 'USD';
  const valuationBySymbol = new Map(
    (valuation?.positions ?? []).map((position) => [position.symbol, position]),
  );
  const pricedPositions = valuation?.positions.filter((position) => position.last_price !== null) ?? [];
  const visiblePriceStates = pricedPositions.length > 0
    ? pricedPositions
    : marketQuote
      ? [marketQuote]
      : [];
  const marketDataStatus = visiblePriceStates.length === 0
    || valuation?.positions.some((position) => position.freshness === 'unavailable')
    ? 'unavailable'
    : visiblePriceStates.some((position) => position.freshness === 'stale')
      ? 'unavailable'
      : visiblePriceStates.some((position) => position.is_synthetic)
      ? 'synthetic'
      : 'live';
  return {
    user,
    setUser,
    checkingSession,
    setCheckingSession,
    verificationToken,
    verificationStatus,
    setVerificationStatus,
    twoFactorStatus,
    setTwoFactorStatus,
    twoFactorSecret,
    setTwoFactorSecret,
    twoFactorProvisioningUri,
    setTwoFactorProvisioningUri,
    twoFactorCode,
    setTwoFactorCode,
    twoFactorBusy,
    setTwoFactorBusy,
    twoFactorError,
    setTwoFactorError,
    onboardingDismissed,
    setOnboardingDismissed,
    registering,
    setRegistering,
    portfolios,
    setPortfolios,
    portfolioId,
    setPortfolioId,
    summary,
    setSummary,
    valuation,
    setValuation,
    riskMetrics,
    setRiskMetrics,
    watchlist,
    setWatchlist,
    coverage,
    setCoverage,
    thesisSymbol,
    setThesisSymbol,
    coverageVersion,
    setCoverageVersion,
    refreshingValuation,
    setRefreshingValuation,
    transactions,
    setTransactions,
    editingTransaction,
    setEditingTransaction,
    csvContent,
    setCsvContent,
    csvPreview,
    setCsvPreview,
    importingCsv,
    setImportingCsv,
    activeView,
    setActiveView,
    openMarketInstrumentId,
    setOpenMarketInstrumentId,
    transactionType,
    setTransactionType,
    journalTrades,
    setJournalTrades,
    journalAnalytics,
    setJournalAnalytics,
    journalBreakdown,
    setJournalBreakdown,
    editingTrade,
    setEditingTrade,
    marketQuery,
    setMarketQuery,
    marketInstruments,
    setMarketInstruments,
    selectedInstrument,
    setSelectedInstrument,
    marketHistory,
    loadingOlderMarketHistory,
    hasOlderMarketHistory,
    setMarketHistory,
    marketIndicators,
    setMarketIndicators,
    marketQuote,
    setMarketQuote,
    marketInterval,
    setMarketInterval,
    marketStart,
    setMarketStart,
    marketEnd,
    setMarketEnd,
    searchingMarket,
    setSearchingMarket,
    loadingMarketHistory,
    setLoadingMarketHistory,
    loadingMarketIndicators,
    setLoadingMarketIndicators,
    loadingMarketQuote,
    setLoadingMarketQuote,
    journalAssetClass,
    setJournalAssetClass,
    journalCurrency,
    setJournalCurrency,
    journalHasExit,
    setJournalHasExit,
    loadingPortfolio,
    setLoadingPortfolio,
    error,
    setError,
    notice,
    setNotice,
    verificationStarted,
    submitAuth,
    createPortfolio,
    submitTransaction,
    loadTransactionCsv,
    previewTransactionCsv,
    importTransactionCsv,
    exportTransactionCsv,
    submitJournalTrade,
    removeJournalTrade,
    refreshJournalTrades,
    removeTransaction,
    startEditingTransaction,
    cancelEditingTransaction,
    startEditingTrade,
    cancelEditingTrade,
    searchMarketInstruments,
    selectMarketInstrument,
    loadInstrumentIndicators,
    loadMarketQuote,
    refreshPortfolioValuation,
    attachSelectedInstrument,
    addSelectedInstrumentToWatchlist,
    addInstrumentToWatchlist,
    removeInstrumentFromWatchlist,
    refreshWatchlist,
    loadMarketHistory,
    loadOlderMarketHistory,
    signOut,
    resendVerificationEmail,
    beginTwoFactorSetup,
    verifyTwoFactor,
    selectedPortfolio,
    currency,
    valuationBySymbol,
    marketDataStatus
  };
}

export type WorkspaceState = ReturnType<typeof useWorkspace>;
