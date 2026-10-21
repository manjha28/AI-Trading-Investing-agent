import { useEffect, useState, type FormEvent } from 'react';
import {
  Activity,
  ArrowDownLeft,
  ArrowUpRight,
  BarChart3,
  CircleAlert,
  CircleDollarSign,
  Clock3,
  Landmark,
  LogOut,
  Pencil,
  Plus,
  ReceiptText,
  Search,
  ShieldCheck,
  Trash2,
  Wallet,
} from 'lucide-react';
import { apiRequest, downloadCsv } from './api';
import { CandleChart } from './CandleChart';
import { BrokersPage } from './BrokersPage';

type User = { id: string; email: string; created_at: string };
type Portfolio = { id: string; name: string; base_currency: string; created_at: string };
type Position = { symbol: string; quantity: string; cost_basis: string; average_cost: string };
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
type TransactionType = 'deposit' | 'withdrawal' | 'dividend' | 'buy' | 'sell';
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
  wins: number;
  losses: number;
  win_rate: string | null;
  net_pnl: string;
  expectancy: string | null;
  profit_factor: string | null;
  max_drawdown: string;
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
type MarketHistory = {
  instrument_id: string;
  interval: string;
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
type MarketQuote = {
  instrument_id: string;
  symbol: string;
  currency: string;
  source: string;
  retrieved_at: string;
  freshness: 'fresh' | 'stale';
  freshness_message: string;
  is_cached: boolean;
  is_synthetic: boolean;
  is_verified: boolean;
  bar: {
    timestamp: string;
    open: string;
    high: string;
    low: string;
    close: string;
    volume: string;
  };
};

function formatMoney(value: string, currency: string): string {
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

function formatQuantity(value: string): string {
  return new Intl.NumberFormat('en-US', { maximumFractionDigits: 8 }).format(Number(value));
}

function localDateTime(value: string | null | undefined): string {
  if (!value) return '';
  const date = new Date(value);
  return new Date(date.getTime() - date.getTimezoneOffset() * 60_000).toISOString().slice(0, 16);
}

function isoDateInput(date: Date): string {
  return date.toISOString().slice(0, 10);
}

function marketRangeParams(interval: string, startDate: string, endDate: string): URLSearchParams {
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

export default function App() {
  const [user, setUser] = useState<User | null>(null);
  const [checkingSession, setCheckingSession] = useState(true);
  const [registering, setRegistering] = useState(false);
  const [portfolios, setPortfolios] = useState<Portfolio[]>([]);
  const [portfolioId, setPortfolioId] = useState('');
  const [summary, setSummary] = useState<PortfolioSummary | null>(null);
  const [valuation, setValuation] = useState<PortfolioValuation | null>(null);
  const [refreshingValuation, setRefreshingValuation] = useState(false);
  const [transactions, setTransactions] = useState<Transaction[]>([]);
  const [editingTransaction, setEditingTransaction] = useState<Transaction | null>(null);
  const [csvContent, setCsvContent] = useState('');
  const [csvPreview, setCsvPreview] = useState<CsvImportPreview | null>(null);
  const [importingCsv, setImportingCsv] = useState(false);
  const [activeView, setActiveView] = useState<'overview' | 'activity' | 'journal' | 'market-data' | 'brokers'>(
    () => new URLSearchParams(window.location.search).has('broker_connected') ? 'brokers' : 'overview',
  );
  const [transactionType, setTransactionType] = useState<TransactionType>('deposit');
  const [journalTrades, setJournalTrades] = useState<JournalTrade[]>([]);
  const [journalAnalytics, setJournalAnalytics] = useState<JournalAnalytics | null>(null);
  const [journalBreakdown, setJournalBreakdown] = useState<JournalBreakdown[]>([]);
  const [editingTrade, setEditingTrade] = useState<JournalTrade | null>(null);
  const [marketQuery, setMarketQuery] = useState('');
  const [marketInstruments, setMarketInstruments] = useState<MarketInstrument[]>([]);
  const [selectedInstrument, setSelectedInstrument] = useState<MarketInstrument | null>(null);
  const [marketHistory, setMarketHistory] = useState<MarketHistory | null>(null);
  const [marketQuote, setMarketQuote] = useState<MarketQuote | null>(null);
  const [marketInterval, setMarketInterval] = useState<'1d' | '1h' | '15m'>('1d');
  const [marketStart, setMarketStart] = useState(
    isoDateInput(new Date(Date.now() - 14 * 24 * 60 * 60 * 1000)),
  );
  const [marketEnd, setMarketEnd] = useState(isoDateInput(new Date()));
  const [searchingMarket, setSearchingMarket] = useState(false);
  const [loadingMarketHistory, setLoadingMarketHistory] = useState(false);
  const [loadingMarketQuote, setLoadingMarketQuote] = useState(false);
  const [journalAssetClass, setJournalAssetClass] = useState('equity');
  const [journalCurrency, setJournalCurrency] = useState('USD');
  const [journalHasExit, setJournalHasExit] = useState(false);
  const [loadingPortfolio, setLoadingPortfolio] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');

  useEffect(() => {
    apiRequest<User>('/auth/me')
      .then(setUser)
      .catch(() => setUser(null))
      .finally(() => setCheckingSession(false));
  }, []);

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
    setEditingTransaction(null);
    setCsvContent('');
    setCsvPreview(null);
  }, [portfolioId]);

  useEffect(() => {
    if (!user || activeView !== 'journal') return;
    let active = true;
    Promise.all([
      apiRequest<JournalTrade[]>('/trades'),
      apiRequest<JournalAnalytics>(`/trades/analytics/summary?currency=${journalCurrency}`),
      apiRequest<JournalBreakdown[]>(`/trades/analytics/breakdown?currency=${journalCurrency}&group_by=strategy`),
    ])
      .then(([trades, analytics, breakdown]) => {
        if (!active) return;
        setJournalTrades(trades);
        setJournalAnalytics(analytics);
        setJournalBreakdown(breakdown);
      })
      .catch((requestError: Error) => {
        if (active) setError(requestError.message);
      });
    return () => {
      active = false;
    };
  }, [activeView, journalCurrency, user]);

  async function submitAuth(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError('');
    const form = new FormData(event.currentTarget);
    try {
      const result = await apiRequest<User>(registering ? '/auth/register' : '/auth/login', {
        method: 'POST',
        body: JSON.stringify({ email: form.get('email'), password: form.get('password') }),
      });
      setUser(result);
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

  async function submitTransaction(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!portfolioId) return;
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
    } catch (requestError) {
      setError((requestError as Error).message);
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
      const [trades, analytics, breakdown] = await Promise.all([
        apiRequest<JournalTrade[]>('/trades'),
        apiRequest<JournalAnalytics>(`/trades/analytics/summary?currency=${journalCurrency}`),
        apiRequest<JournalBreakdown[]>(`/trades/analytics/breakdown?currency=${journalCurrency}&group_by=strategy`),
      ]);
      setJournalTrades(trades);
      setJournalAnalytics(analytics);
      setJournalBreakdown(breakdown);
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
      const [trades, analytics, breakdown] = await Promise.all([
        apiRequest<JournalTrade[]>('/trades'),
        apiRequest<JournalAnalytics>(`/trades/analytics/summary?currency=${journalCurrency}`),
        apiRequest<JournalBreakdown[]>(`/trades/analytics/breakdown?currency=${journalCurrency}&group_by=strategy`),
      ]);
      setJournalTrades(trades);
      setJournalAnalytics(analytics);
      setJournalBreakdown(breakdown);
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

  async function selectMarketInstrument(instrument: MarketInstrument) {
    setError('');
    setMarketHistory(null);
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
    } catch (requestError) {
      setError((requestError as Error).message);
    }
  }

  async function loadMarketQuote(instrument = selectedInstrument, force = false) {
    if (!instrument) return;
    setError('');
    setMarketQuote(null);
    setLoadingMarketQuote(true);
    try {
      const quote = await apiRequest<MarketQuote>(
        `/market-data/instruments/${instrument.id}/quote${force ? '?refresh=true' : ''}`,
      );
      setMarketQuote(quote);
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
    try {
      await apiRequest<void>(`/market-data/watchlist/${selectedInstrument.id}`, { method: 'POST' });
      setNotice(`${selectedInstrument.symbol} added to the quote-refresh watchlist.`);
    } catch (requestError) {
      setError((requestError as Error).message);
    }
  }

  async function loadMarketHistory() {
    if (!selectedInstrument || !marketStart || !marketEnd) return;
    setError('');
    setMarketHistory(null);
    setLoadingMarketHistory(true);
    try {
      const params = marketRangeParams(marketInterval, marketStart, marketEnd);
      const history = await apiRequest<MarketHistory>(
        `/market-data/instruments/${selectedInstrument.id}/history/sync?${params}`,
        { method: 'POST' },
      );
      setMarketHistory(history);
    } catch (requestError) {
      setError((requestError as Error).message);
      if (selectedInstrument) {
        try {
          const params = marketRangeParams(marketInterval, marketStart, marketEnd);
          const history = await apiRequest<MarketHistory>(
            `/market-data/instruments/${selectedInstrument.id}/history?${params}`,
          );
          setMarketHistory(history);
        } catch (historyError) {
          setError(`${(requestError as Error).message}; saved history unavailable: ${(historyError as Error).message}`);
        }
      }
    } finally {
      setLoadingMarketHistory(false);
    }
  }

  async function cleanupSyntheticInstruments() {
    try {
      const result = await apiRequest<{
        deleted_unlinked: number;
        marked_for_relink: number;
        deleted_ids: string[];
        relink_ids: string[];
        message: string;
      }>(
        '/market-data/instruments/cleanup-synthetic',
        { method: 'POST' },
      );
      setNotice(`${result.message} Removed ${result.deleted_unlinked}; marked ${result.marked_for_relink} for relinking.`);
      if (selectedInstrument && result.deleted_ids.includes(selectedInstrument.id)) {
        setSelectedInstrument(null);
        setMarketHistory(null);
        setMarketQuote(null);
      } else if (selectedInstrument && result.relink_ids.includes(selectedInstrument.id)) {
        const updated = await apiRequest<MarketInstrument>(`/market-data/instruments/${selectedInstrument.id}`);
        setSelectedInstrument(updated);
      }
    } catch (requestError) {
      setError((requestError as Error).message);
    }
  }

  async function signOut() {
    setError('');
    setNotice('');
    try {
      await apiRequest<void>('/auth/logout', { method: 'POST' });
      setUser(null);
      setPortfolios([]);
      setPortfolioId('');
    } catch (requestError) {
      setError((requestError as Error).message);
    }
  }

  if (checkingSession) {
    return <main className="loading-screen"><Activity size={20} /> Checking secure session</main>;
  }

  if (!user) {
    return (
      <main className="auth-layout">
        <section className="auth-panel">
          <div className="brand-lockup"><span className="brand-symbol"><Activity size={20} /></span><span>Trading Copilot</span></div>
          <p className="eyebrow">PERSONAL INVESTMENT WORKSPACE</p>
          <h1>{registering ? 'Create your account' : 'Sign in'}</h1>
          <form className="form-stack" onSubmit={submitAuth}>
            <label>Email address<input name="email" type="email" autoComplete="email" required /></label>
            <label>Password<input name="password" type="password" minLength={12} autoComplete={registering ? 'new-password' : 'current-password'} required /></label>
            <button className="primary-button" type="submit">{registering ? 'Create account' : 'Sign in'}</button>
          </form>
          <button className="secondary-button" type="button" onClick={() => void cleanupSyntheticInstruments()}>Clean up synthetic demo instruments</button>
          {error && <p className="error-message" role="alert"><CircleAlert size={16} />{error}</p>}
          <button className="text-button" type="button" onClick={() => { setError(''); setRegistering((value) => !value); }}>
            {registering ? 'Already have an account? Sign in' : 'Create an account'}
          </button>
          <p className="security-note"><ShieldCheck size={15} /> Secure server-side sessions · no broker connection</p>
        </section>
      </main>
    );
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
  return (
    <div className="app-frame">
      <aside className="sidebar">
        <div className="brand-lockup"><span className="brand-symbol"><Activity size={20} /></span><span>Trading Copilot</span></div>
        <p className="nav-label">WORKSPACE</p>
        <nav className="primary-nav" aria-label="Workspace">
          <button className={activeView === 'overview' ? 'nav-item active' : 'nav-item'} onClick={() => setActiveView('overview')}><Wallet size={17} /> Overview</button>
          <button className={activeView === 'activity' ? 'nav-item active' : 'nav-item'} onClick={() => setActiveView('activity')}><ReceiptText size={17} /> Transactions</button>
          <button className={activeView === 'journal' ? 'nav-item active' : 'nav-item'} onClick={() => setActiveView('journal')}><Activity size={17} /> Trade journal</button>
          <button className={activeView === 'market-data' ? 'nav-item active' : 'nav-item'} onClick={() => setActiveView('market-data')}><BarChart3 size={17} /> Market data</button>
          <button className={activeView === 'brokers' ? 'nav-item active' : 'nav-item'} onClick={() => setActiveView('brokers')}><Landmark size={17} /> Brokers</button>
        </nav>
        <div className="sidebar-status"><span className={`status-dot ${marketDataStatus}`} /> Paper ledger only<span>Market data: {marketDataStatus}</span></div>
        <button className="signout-button" onClick={signOut}><LogOut size={16} /> Sign out</button>
      </aside>

      <main className="workspace">
        <header className="workspace-header">
          <div><p className="eyebrow">PORTFOLIO WORKSPACE</p><h1>{activeView === 'overview' ? 'Overview' : activeView === 'journal' ? 'Trade journal' : activeView === 'market-data' ? 'Market data' : activeView === 'brokers' ? 'Connect Broker' : 'Transactions'}</h1></div>
          <div className="header-actions">
            {portfolios.length > 0 && <label className="portfolio-picker"><span>Portfolio</span><select value={portfolioId} onChange={(event) => setPortfolioId(event.target.value)}>{portfolios.map((portfolio) => <option key={portfolio.id} value={portfolio.id}>{portfolio.name}</option>)}</select></label>}
            <span className={`market-status market-status-${marketDataStatus}`} role="status">Market data: {marketDataStatus}</span>
            <span className="user-chip">{user.email}</span>
          </div>
        </header>

        {error && <p className="error-banner" role="alert"><CircleAlert size={17} />{error}<button type="button" onClick={() => setError('')} aria-label="Dismiss error">×</button></p>}
        {notice && <p className="success-banner" role="status"><ShieldCheck size={17} />{notice}<button type="button" onClick={() => setNotice('')} aria-label="Dismiss notice">×</button></p>}

        {activeView === 'brokers' ? (
          <BrokersPage portfolioId={portfolioId} />
        ) : activeView === 'market-data' ? (
          <section className="market-page">
            <div className="data-note"><CircleDollarSign size={17} /><span>Market data provenance and freshness are shown for each result. Provider mappings are not verified exchange listings unless explicitly marked; market data is not used for portfolio valuation.</span></div>
            <div className="content-grid market-grid">
              <section className="data-section">
                <div className="section-heading"><div><p className="eyebrow">INSTRUMENT MASTER</p><h2>Search instruments</h2></div><Search size={18} /></div>
                <form className="market-search" onSubmit={searchMarketInstruments}>
                  <label>Symbol, name, or venue<input value={marketQuery} onChange={(event) => setMarketQuery(event.target.value)} minLength={1} maxLength={80} placeholder="AAPL, RELIANCE, BTCUSD" required /></label>
                  <button className="primary-button" type="submit" disabled={searchingMarket}><Search size={15} />{searchingMarket ? 'Searching…' : 'Search'}</button>
                </form>
                {marketInstruments.length === 0 ? <div className="table-empty">{searchingMarket ? 'Searching instrument master…' : 'Search available equities and crypto markets.'}</div> : (
                  <div className="market-instrument-list" aria-label="Instrument search results">
                    {marketInstruments.map((instrument) => <button className={selectedInstrument?.id === instrument.id ? 'market-instrument selected' : 'market-instrument'} type="button" key={instrument.id} onClick={() => void selectMarketInstrument(instrument)}>
                      <span><strong>{instrument.symbol}</strong><small>{instrument.display_name}</small></span><span className="market-instrument-meta">{instrument.venue} · {instrument.currency}<small>{instrument.asset_class}</small></span>
                    </button>)}
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
                  <button className="secondary-button" type="button" onClick={() => void addSelectedInstrumentToWatchlist()}>Add to refresh watchlist</button>
                  <button className="secondary-button" type="button" onClick={() => void loadMarketQuote(selectedInstrument, true)} disabled={loadingMarketQuote}>{loadingMarketQuote ? 'Loading quote…' : 'Refresh instrument price'}</button>
                  {marketQuote && <div className="market-quote" aria-live="polite">
                    <div className="section-heading"><div><p className="eyebrow">LATEST PRICE</p><h3>{formatMoney(marketQuote.bar.close, marketQuote.currency)}</h3></div><span className={`freshness-badge ${marketQuote.is_synthetic ? 'freshness-synthetic' : `freshness-${marketQuote.freshness}`}`}>{marketQuote.is_synthetic ? 'synthetic' : marketQuote.freshness}</span></div>
                    <p className="market-provenance">{marketQuote.freshness_message} Source: {marketQuote.source} · Retrieved: {new Date(marketQuote.retrieved_at).toLocaleString()} · {marketQuote.is_cached ? 'Cached' : 'Provider response'} · {marketQuote.is_synthetic ? 'Synthetic' : marketQuote.is_verified ? 'Verified' : 'Unverified'}</p>
                  </div>}
                  <div className="market-range">
                      <label>Interval<select value={marketInterval} onChange={(event) => { setMarketInterval(event.target.value as '1d' | '1h' | '15m'); setMarketHistory(null); }}><option value="1d">1 day</option><option value="1h">1 hour</option><option value="15m">15 minutes</option></select></label>
                      <label>From<input type="date" value={marketStart} onChange={(event) => { setMarketStart(event.target.value); setMarketHistory(null); }} required /></label>
                      <label>Through<input type="date" value={marketEnd} onChange={(event) => { setMarketEnd(event.target.value); setMarketHistory(null); }} required /></label>
                  </div>
                  <button className="primary-button" type="button" onClick={loadMarketHistory} disabled={loadingMarketHistory}>{loadingMarketHistory ? 'Loading history…' : 'Load history'}</button>
                </> : <p className="table-empty">Choose a search result to inspect instrument details and available bars.</p>}
              </section>
            </div>
            {marketHistory && selectedInstrument && <section className="data-section market-history">
              <div className="section-heading"><div><p className="eyebrow">PROVENANCE &amp; FRESHNESS</p><h2>{selectedInstrument.symbol} history</h2></div><span className={`freshness-badge freshness-${marketHistory.freshness.status}`}>{marketHistory.freshness.status.replace('_', ' ')}</span></div>
              <p className="market-provenance">{marketHistory.freshness.message} Source: {marketHistory.freshness.source ?? '—'} · Last successful fetch: {marketHistory.freshness.last_successful_update ? new Date(marketHistory.freshness.last_successful_update).toLocaleString() : 'Never'} · Latest bar: {marketHistory.freshness.latest_bar_timestamp ? new Date(marketHistory.freshness.latest_bar_timestamp).toLocaleString() : 'Unavailable'}</p>
              {marketHistory.bars.length === 0 ? <div className="table-empty">No bars are available for that date range.</div> : <>
                <CandleChart bars={marketHistory.bars} currency={selectedInstrument.currency} interval={marketInterval} />
                <div className="table-scroll"><table><thead><tr><th>Timestamp (UTC)</th><th>Open</th><th>High</th><th>Low</th><th>Close</th><th>Volume</th><th>Source</th></tr></thead><tbody>{marketHistory.bars.map((bar) => {
                  const verified = selectedInstrument.provider_identifiers.find((mapping) => mapping.provider === bar.source)?.is_verified === true;
                  return <tr key={`${bar.timestamp}-${bar.source}`}><td>{new Date(bar.timestamp).toISOString().replace('T', ' ').replace('.000Z', 'Z')}</td><td>{Number(bar.open).toFixed(2)}</td><td>{Number(bar.high).toFixed(2)}</td><td>{Number(bar.low).toFixed(2)}</td><td>{Number(bar.close).toFixed(2)}</td><td>{bar.volume}</td><td>{bar.is_synthetic ? 'Synthetic · ' : ''}{bar.is_synthetic || !verified ? 'Unverified · ' : 'Verified · '}{bar.source}</td></tr>;
                })}</tbody></table></div>
              </>}
            </section>}
          </section>
        ) : activeView === 'journal' ? (
          <>
            <div className="data-note"><CircleDollarSign size={17} /><span>Journal analytics use only entered trades, fees, multipliers, and timestamps. No market prices are fetched.</span></div>
            {journalAnalytics && <section className="metrics-row journal-metrics" aria-label="Journal performance">
              <article className="metric"><span>Closed trades</span><strong>{journalAnalytics.closed_trades}</strong><small>{journalAnalytics.open_trades} still open</small></article>
              <article className="metric"><span>Win rate</span><strong>{journalAnalytics.win_rate === null ? '—' : `${journalAnalytics.win_rate}%`}</strong><small>{journalAnalytics.wins} wins · {journalAnalytics.losses} losses</small></article>
              <article className="metric"><span>Net realized P&amp;L · {journalCurrency}</span><strong className={Number(journalAnalytics.net_pnl) >= 0 ? 'positive' : 'negative'}>{formatMoney(journalAnalytics.net_pnl, journalCurrency)}</strong><small>Closed trades in this currency</small></article>
              <article className="metric"><span>Max drawdown · {journalCurrency}</span><strong>{formatMoney(journalAnalytics.max_drawdown, journalCurrency)}</strong><small>Closed-trade sequence</small></article>
            </section>}
            <section className="data-section breakdown-section">
              <div className="section-heading"><div><p className="eyebrow">PERFORMANCE BY STRATEGY</p><h2>Strategy breakdown</h2></div></div>
              {journalBreakdown.length === 0 ? <div className="table-empty">No strategy results for this currency.</div> : <div className="table-scroll"><table><thead><tr><th>Strategy</th><th>Closed</th><th>Wins / losses</th><th>Net P&amp;L</th><th>Expectancy</th><th>Profit factor</th></tr></thead><tbody>{journalBreakdown.map((row) => <tr key={row.key}><td className="instrument-cell">{row.key}</td><td>{row.closed_trades}</td><td>{row.wins} / {row.losses}</td><td className={Number(row.net_pnl) >= 0 ? 'positive' : 'negative'}>{formatMoney(row.net_pnl, journalCurrency)}</td><td>{row.expectancy === null ? '—' : formatMoney(row.expectancy, journalCurrency)}</td><td>{row.profit_factor ?? '—'}</td></tr>)}</tbody></table></div>}
            </section>
            <div className="content-grid journal-grid">
              <section className="data-section">
                <div className="section-heading"><div><p className="eyebrow">TRADE HISTORY</p><h2>Recorded trades</h2></div><label className="journal-currency">Analytics currency<select value={journalCurrency} onChange={(event) => setJournalCurrency(event.target.value)}><option value="USD">USD</option><option value="INR">INR</option></select></label></div>
                {journalTrades.length === 0 ? <div className="table-empty">No journal trades recorded.</div> : <div className="table-scroll"><table><thead><tr><th>Instrument</th><th>Market</th><th>Side</th><th>Size</th><th>Entry</th><th>Exit</th><th>Net P&amp;L</th><th /></tr></thead><tbody>{journalTrades.map((trade) => <tr key={trade.id}><td className="instrument-cell">{trade.instrument}<small className="trade-subtext">{trade.strategy || 'No strategy'}</small></td><td>{trade.asset_class}</td><td>{trade.side}</td><td>{formatQuantity(trade.quantity)} × {formatQuantity(trade.multiplier)}</td><td>{formatMoney(trade.entry_price, trade.currency)}</td><td>{trade.exit_price === null ? 'Open' : formatMoney(trade.exit_price, trade.currency)}</td><td className={trade.net_pnl === null ? '' : Number(trade.net_pnl) >= 0 ? 'positive' : 'negative'}>{trade.net_pnl === null ? '—' : formatMoney(trade.net_pnl, trade.currency)}</td><td><button className="icon-button" type="button" onClick={() => startEditingTrade(trade)} aria-label={`Edit ${trade.instrument} trade`}><Pencil size={15} /></button><button className="icon-button" type="button" onClick={() => removeJournalTrade(trade.id)} aria-label={`Delete ${trade.instrument} trade`}><Trash2 size={15} /></button></td></tr>)}</tbody></table></div>}
                {journalAnalytics && <div className="valuation-footnote">Expectancy {journalAnalytics.expectancy === null ? '—' : formatMoney(journalAnalytics.expectancy, journalCurrency)} · Profit factor {journalAnalytics.profit_factor ?? '—'}</div>}
              </section>
              <section className="entry-section">
                <div className="section-heading"><div><p className="eyebrow">{editingTrade ? 'EDIT ENTRY' : 'MANUAL ENTRY'}</p><h2>{editingTrade ? `Edit ${editingTrade.instrument}` : 'Log a trade'}</h2></div>{editingTrade ? <button className="text-button" type="button" onClick={cancelEditingTrade}>Cancel</button> : <Plus size={18} />}</div>
                <form key={editingTrade?.id ?? 'new-trade'} className="form-stack compact" onSubmit={submitJournalTrade}>
                  <label>Market<select name="asset_class" value={journalAssetClass} onChange={(event) => setJournalAssetClass(event.target.value)}><option value="equity">Equity</option><option value="future">Future</option><option value="option">Option</option><option value="crypto">Crypto</option></select></label>
                  <label>Quote currency<select name="currency" defaultValue={editingTrade?.currency ?? 'USD'}><option value="USD">USD</option><option value="INR">INR</option></select></label>
                  <label>Instrument<input name="instrument" placeholder="Symbol or contract" maxLength={64} defaultValue={editingTrade?.instrument ?? ''} required /></label>
                  <div className="form-pair"><label>Side<select name="side" defaultValue={editingTrade?.side ?? 'long'}><option value="long">Long</option><option value="short">Short</option></select></label><label>Quantity<input name="quantity" type="number" min="0.00000001" step="0.00000001" defaultValue={editingTrade?.quantity ?? ''} required /></label></div>
                  <div className="form-pair"><label>Multiplier<input name="multiplier" type="number" min="0.00000001" step="0.00000001" defaultValue={editingTrade?.multiplier ?? '1'} required /></label><label>Entry price<input name="entry_price" type="number" min="0.00000001" step="0.00000001" defaultValue={editingTrade?.entry_price ?? ''} required /></label></div>
                  <label>Entry time<input name="entry_time" type="datetime-local" defaultValue={localDateTime(editingTrade?.entry_time) || new Date(Date.now() - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 16)} required /></label>
                  {journalHasExit && <><div className="form-pair"><label>Exit price<input name="exit_price" type="number" min="0.00000001" step="0.00000001" defaultValue={editingTrade?.exit_price ?? ''} required /></label><label>Exit time<input name="exit_time" type="datetime-local" defaultValue={localDateTime(editingTrade?.exit_time)} required /></label></div></>}
                  {journalAssetClass === 'option' && <><div className="form-pair"><label>Option type<select name="option_type" defaultValue={editingTrade?.option_type ?? 'call'}><option value="call">Call</option><option value="put">Put</option></select></label><label>Strike<input name="strike" type="number" min="0.00000001" step="0.00000001" defaultValue={editingTrade?.strike ?? ''} required /></label></div><label>Expiry<input name="expiry" type="date" defaultValue={editingTrade?.expiry?.slice(0, 10) ?? ''} required /></label></>}
                  <label>Fees paid<input name="fees" type="number" min="0" step="0.01" defaultValue={editingTrade?.fees ?? '0.00'} required /></label>
                  <label>Planned risk<input name="planned_risk" type="number" min="0.01" step="0.01" defaultValue={editingTrade?.planned_risk ?? ''} /></label>
                  <label>Strategy<input name="strategy" maxLength={100} defaultValue={editingTrade?.strategy ?? ''} /></label>
                  <label>Setup<input name="setup" maxLength={100} defaultValue={editingTrade?.setup ?? ''} /></label>
                  <label>Tags<input name="tags" placeholder="patient, breakout" defaultValue={editingTrade?.tags.join(', ') ?? ''} /></label>
                  <label>Notes<textarea name="notes" maxLength={4000} defaultValue={editingTrade?.notes ?? ''} rows={3} /></label>
                  <label className="checkbox-line"><input type="checkbox" checked={journalHasExit} onChange={(event) => setJournalHasExit(event.target.checked)} /> Trade is closed</label>
                  <button className="primary-button" type="submit"><Plus size={16} /> {editingTrade ? 'Update trade' : 'Save trade'}</button>
                </form>
              </section>
            </div>
          </>
        ) : portfolios.length === 0 ? (
          <section className="empty-workspace">
            <div className="section-icon"><Wallet size={22} /></div>
            <p className="eyebrow">FIRST PORTFOLIO</p>
            <h2>Start with a clean ledger.</h2>
            <p>Balances and positions will be derived only from the transactions you enter.</p>
            <form className="portfolio-create" onSubmit={createPortfolio}>
              <label>Portfolio name<input name="name" placeholder="Long-term account" maxLength={100} required /></label>
              <label>Base currency<select name="base_currency"><option value="USD">USD · US dollar</option><option value="INR">INR · Indian rupee</option></select></label>
              <button className="primary-button" type="submit"><Plus size={17} /> Create portfolio</button>
            </form>
          </section>
        ) : (
          <>
            <div className="valuation-toolbar">
              <div className="data-note"><CircleDollarSign size={17} /><span>{valuation?.confidence === 'unreliable' ? valuation.reasons.join(' ') : 'Portfolio prices are shown with freshness and source provenance; synthetic data is never presented as live.'}</span></div>
              <button className="secondary-button" type="button" onClick={() => void refreshPortfolioValuation()} disabled={refreshingValuation || !portfolioId}>
                {refreshingValuation ? 'Refreshing…' : 'Refresh prices'}
              </button>
              <span className="last-updated">Updated {valuation?.last_updated ? new Date(valuation.last_updated).toLocaleString() : '—'}</span>
            </div>
            {loadingPortfolio || !summary ? <p className="loading-inline">Loading persisted portfolio data…</p> : (
              <>
                <section className="metrics-row portfolio-metrics" aria-label="Portfolio balances">
                  <article className="metric"><span>Cash balance</span><strong>{formatMoney(summary.cash_balance, currency)}</strong><small>After recorded fees</small></article>
                  <article className="metric"><span>Market value</span><strong>{valuation ? formatMoney(valuation.market_value, currency) : 'Unavailable'}</strong><small>{valuation?.confidence === 'unreliable' ? 'Partial or synthetic prices' : `${summary.positions.length} open positions`}</small></article>
                  <article className="metric"><span>Total value</span><strong>{valuation ? formatMoney(valuation.total_value, currency) : 'Unavailable'}</strong><small>Cash + priced holdings · no FX</small></article>
                  <article className="metric"><span>Unrealized P&amp;L</span><strong className={valuation && Number(valuation.unrealized_pnl) >= 0 ? 'positive' : 'negative'}>{valuation ? formatMoney(valuation.unrealized_pnl, currency) : 'Unavailable'}</strong><small>{valuation?.confidence === 'unreliable' ? 'Unreliable valuation' : 'Based on last prices'}</small></article>
                  <article className="metric"><span>Day change</span><strong className={valuation && Number(valuation.day_change) >= 0 ? 'positive' : 'negative'}>{valuation ? formatMoney(valuation.day_change, currency) : 'Unavailable'}</strong><small>Quoted open-to-last move</small></article>
                  <article className="metric"><span>Realized P&amp;L</span><strong className={Number(summary.realized_pnl) >= 0 ? 'positive' : 'negative'}>{formatMoney(summary.realized_pnl, currency)}</strong><small>Sales and recorded cash income</small></article>
                </section>

                <div className="content-grid">
                  <section className="data-section">
                    <div className="section-heading"><div><p className="eyebrow">{activeView === 'overview' ? 'CURRENT EXPOSURE' : 'LEDGER'}</p><h2>{activeView === 'overview' ? 'Open positions' : 'Transaction history'}</h2></div><span className="section-count">{activeView === 'overview' ? summary.positions.length : transactions.length} records</span></div>
                    {activeView === 'overview' ? (
                      summary.positions.length === 0 ? <div className="table-empty">No open positions recorded.</div> : (
                        <div className="table-scroll"><table><thead><tr><th>Instrument</th><th>Quantity</th><th>Average cost</th><th>Cost basis</th><th>Last price</th><th>Market value</th><th>Unrealized P&amp;L</th><th>Day change</th><th>Weight</th><th>Data quality</th></tr></thead><tbody>{summary.positions.map((position) => {
                          const priced = valuationBySymbol.get(position.symbol);
                          const needsAttention = !priced || priced.freshness === 'stale' || priced.is_synthetic || priced.freshness === 'unavailable';
                          return <tr key={position.symbol}>
                            <td className="instrument-cell">{position.symbol}</td>
                            <td>{formatQuantity(position.quantity)}</td>
                            <td>{formatMoney(position.average_cost, currency)}</td>
                            <td>{formatMoney(position.cost_basis, currency)}</td>
                            <td>{priced?.last_price ? formatMoney(priced.last_price, currency) : '—'}</td>
                            <td>{priced?.market_value ? formatMoney(priced.market_value, currency) : '—'}</td>
                            <td className={priced?.unrealized_pnl === null || priced?.unrealized_pnl === undefined ? '' : Number(priced.unrealized_pnl) >= 0 ? 'positive' : 'negative'}>{priced?.unrealized_pnl ? formatMoney(priced.unrealized_pnl, currency) : '—'}</td>
                            <td>{priced?.day_change ? formatMoney(priced.day_change, currency) : '—'}{priced?.day_change_percent ? ` (${Number(priced.day_change_percent).toFixed(2)}%)` : ''}</td>
                            <td>{priced?.weight_percent ? `${Number(priced.weight_percent).toFixed(2)}%` : '—'}</td>
                            <td>{needsAttention ? <span className={`freshness-badge ${priced?.is_synthetic ? 'freshness-synthetic' : `freshness-${priced?.freshness ?? 'unavailable'}`}`}>{priced?.is_synthetic ? 'synthetic' : priced?.freshness ?? 'unlinked'}</span> : <span className="freshness-badge freshness-fresh">live</span>}</td>
                          </tr>;
                        })}</tbody></table></div>
                      )
                    ) : (
                      transactions.length === 0 ? <div className="table-empty">No transactions recorded.</div> : (
                        <div className="table-scroll"><table><thead><tr><th>Type</th><th>Instrument</th><th>Quantity</th><th>Gross amount</th><th>Fee</th><th>Event time</th><th /></tr></thead><tbody>{[...transactions].reverse().map((transaction) => <tr key={transaction.id}><td><span className={`type-label type-${transaction.transaction_type}`}>{transaction.transaction_type}</span></td><td>{transaction.symbol ?? 'Cash'}</td><td>{transaction.quantity ? formatQuantity(transaction.quantity) : '—'}</td><td>{formatMoney(transaction.amount, currency)}</td><td>{formatMoney(transaction.fee, currency)}</td><td>{new Date(transaction.occurred_at).toLocaleString()}</td><td><button className="icon-button" type="button" onClick={() => startEditingTransaction(transaction)} aria-label={`Edit ${transaction.transaction_type} transaction`}><Pencil size={15} /></button><button className="icon-button" type="button" onClick={() => removeTransaction(transaction.id)} aria-label={`Delete ${transaction.transaction_type} transaction`}><Trash2 size={15} /></button></td></tr>)}</tbody></table></div>
                      )
                    )}
                    <div className="valuation-footnote"><span className={`status-dot ${marketDataStatus}`} /> {valuation?.confidence === 'unreliable' ? `Valuation is unreliable: ${valuation.reasons.join(' ')}` : `Open cost basis ${formatMoney(summary.invested_cost, currency)} · market prices are not FX-converted.`}</div>
                  </section>

                  <section className="entry-section">
                    <div className="section-heading"><div><p className="eyebrow">{editingTransaction ? 'EDIT ENTRY' : 'MANUAL ENTRY'}</p><h2>{editingTransaction ? 'Edit activity' : 'Record activity'}</h2></div>{editingTransaction ? <button className="text-button" type="button" onClick={cancelEditingTransaction}>Cancel</button> : <Plus size={18} />}</div>
                    <form key={editingTransaction?.id ?? 'new-transaction'} className="form-stack compact" onSubmit={submitTransaction}>
                      <label>Activity type<select name="transaction_type" value={transactionType} onChange={(event) => setTransactionType(event.target.value as TransactionType)}><option value="deposit">Deposit</option><option value="withdrawal">Withdrawal</option><option value="dividend">Dividend</option><option value="buy">Buy equity</option><option value="sell">Sell equity</option></select></label>
                      {['buy', 'sell', 'dividend'].includes(transactionType) && <label>Symbol<input name="symbol" placeholder="e.g. AAPL" maxLength={32} defaultValue={editingTransaction?.symbol ?? ''} required /></label>}
                      {['buy', 'sell'].includes(transactionType) && <>
                        <label>Quantity<input name="quantity" type="number" min="0.00000001" step="0.00000001" defaultValue={editingTransaction?.quantity ?? ''} required /></label>
                        <label>Unit price<input name="unit_price" type="number" min="0.00000001" step="0.00000001" defaultValue={editingTransaction?.unit_price ?? ''} required /></label>
                      </>}
                      {!['buy', 'sell'].includes(transactionType) && <label>Gross amount<input name="amount" type="number" min="0.01" step="0.01" defaultValue={editingTransaction?.amount ?? ''} required /></label>}
                      <label>Fee<input name="fee" type="number" min="0" step="0.01" defaultValue={editingTransaction?.fee ?? '0.00'} required /></label>
                      <label>Event time<input name="occurred_at" type="datetime-local" defaultValue={localDateTime(editingTransaction?.occurred_at) || new Date(Date.now() - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 16)} required /></label>
                      <button className="primary-button" type="submit"><ArrowUpRight size={17} /> {editingTransaction ? 'Update transaction' : 'Save transaction'}</button>
                    </form>
                    <div className="csv-tools">
                      <p className="eyebrow">CSV</p>
                      <button className="secondary-button" type="button" onClick={exportTransactionCsv}>Export transactions</button>
                      <label className="csv-file">Import file<input type="file" accept=".csv,text/csv" onChange={(event) => void loadTransactionCsv(event.target.files?.[0])} /></label>
                      <button className="secondary-button" type="button" onClick={previewTransactionCsv} disabled={!csvContent}>Preview import</button>
                      {csvPreview && <>
                        <p className="csv-summary">{csvPreview.valid_count} ready · {csvPreview.duplicate_count} duplicate · {csvPreview.invalid_count} invalid</p>
                        {csvPreview.rows.some((row) => row.status !== 'valid') && <ul className="csv-preview" aria-label="CSV row validation results">
                          {csvPreview.rows.filter((row) => row.status !== 'valid').slice(0, 8).map((row) => (
                            <li key={row.row_number} className={row.status}>
                              Row {row.row_number}: {row.status === 'duplicate' ? 'already recorded' : row.errors.join(', ')}
                            </li>
                          ))}
                        </ul>}
                        <button className="primary-button" type="button" onClick={importTransactionCsv} disabled={csvPreview.valid_count === 0 || csvPreview.invalid_count > 0 || importingCsv}>
                          {importingCsv ? 'Importing…' : `Import ${csvPreview.valid_count} transaction${csvPreview.valid_count === 1 ? '' : 's'}`}
                        </button>
                      </>}
                      <p className="accounting-note">Expected columns: transaction_type, symbol, quantity, unit_price, amount, fee, occurred_at. Preview is required; duplicate rows are skipped.</p>
                    </div>
                    <p className="accounting-note"><ShieldCheck size={15} /> Long-only cash equities. Fees and cost basis use {currency} minor units; no FX conversion.</p>
                  </section>
                </div>
              </>
            )}
          </>
        )}
        <footer className="workspace-footer"><span><ArrowDownLeft size={14} /> No live orders</span><span>Data entered by you · {portfolios.length} portfolio{portfolios.length === 1 ? '' : 's'}</span></footer>
      </main>
      <nav className="mobile-nav" aria-label="Workspace">
        <button className={activeView === 'overview' ? 'nav-item active' : 'nav-item'} onClick={() => setActiveView('overview')}><Wallet size={18} /><span>Overview</span></button>
        <button className={activeView === 'activity' ? 'nav-item active' : 'nav-item'} onClick={() => setActiveView('activity')}><ReceiptText size={18} /><span>Transactions</span></button>
        <button className={activeView === 'journal' ? 'nav-item active' : 'nav-item'} onClick={() => setActiveView('journal')}><Activity size={18} /><span>Journal</span></button>
        <button className={activeView === 'market-data' ? 'nav-item active' : 'nav-item'} onClick={() => setActiveView('market-data')}><BarChart3 size={18} /><span>Markets</span></button>
        <button className={activeView === 'brokers' ? 'nav-item active' : 'nav-item'} onClick={() => setActiveView('brokers')}><Landmark size={18} /><span>Brokers</span></button>
      </nav>
    </div>
  );
}
