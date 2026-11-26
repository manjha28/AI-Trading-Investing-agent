import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';

vi.mock('../api', () => ({ apiRequest: vi.fn(), ApiRequestError: class extends Error {} }));
vi.mock('../pages/Strategies', () => ({ Strategies: () => <div>legacy page</div> }));

import { apiRequest } from '../api';
import { SignalsWorkspace } from './SignalsWorkspace';
import css from './signals.css?raw';

const api = vi.mocked(apiRequest);
type Call = { path: string; method: string; body: unknown };
let calls: Call[] = [];
let overrides: Record<string, (body: unknown) => unknown> = {};

const indicator = (id: string, name: string, category: string, outputs = ['value']) => ({
  id, name, category, outputs, formula: `${name} formula`, notes: '', uses_source: false, minimum_bars_default: 20,
  params: { length: { default: 20, min: 1, max: 500, kind: 'int', choices: [] } },
});
const META = {
  schema_version: 'intraday-strategy/1', engine_version: 'intraday-engine/1.0.0', indicator_library_version: '1.0.0',
  timeframes: ['1m', '3m', '5m', '10m', '15m', '30m', '60m'], markets: ['equity_in'], price_fields: ['open', 'high', 'low', 'close', 'volume'],
  compare_ops: ['>', '>=', '<', '<=', '==', '!=', 'crosses_above', 'crosses_below'],
  indicators: [indicator('ema', 'Exponential Moving Average', 'trend'), indicator('rsi', 'Relative Strength Index', 'momentum'), indicator('macd', 'MACD', 'momentum', ['macd', 'signal', 'hist'])],
  cost_profiles: [{ id: 'in_equity_intraday_2024_10', label: 'India equity intraday', market: 'equity_in', effective_from: '2024-10-01', assumed: true, source_note: 'assumed', params: { brokerage_flat: '20' } }],
  pine: { supported: ['ta.ema'], partial: ['plot (display only)'], unsupported: ['strategy.exit'] },
  python: { template: 'class Strategy: pass', allowed_imports: ['math'], timeout_seconds: 20, memory_mb: 512, isolation: 'subprocess + AST screen' },
  backtest_defaults: {}, disclaimer: 'Decision support, not investment advice.',
};
const INTRADAY = {
  id: 's1', name: 'EMA cross', description: 'd', status: 'active', current_version: 2, instrument_id: 'i1', kind: 'intraday', source_type: 'visual', market: 'equity_in',
  decision_timeframe: '5m', created_at: '2025-01-01T00:00:00Z', updated_at: '2025-01-02T00:00:00Z', params: null,
  last_backtest: { id: 'r0', at: '2025-01-03T00:00:00Z', version: 2, trades: 12, net_profit: '1200.00', return_pct: 0.12 },
};
const LEGACY = { ...INTRADAY, id: 'l1', name: 'Reliance MA', status: 'retired', kind: 'legacy_daily', source_type: 'legacy', decision_timeframe: '1d', current_version: 1, last_backtest: null, params: { fast_period: 20, slow_period: 50 } };
const SUSPENDED = { ...LEGACY, id: 'l2', name: 'test2', status: 'suspended' };
const DEF = {
  schema_version: 'intraday-strategy/1', name: 'EMA cross', description: '', market: 'equity_in', universe: ['i1'], base_timeframe: '1m', decision_timeframe: '5m',
  indicators: [{ alias: 'f', indicator: 'ema', params: { length: 9 }, timeframe: '5m' }, { alias: 's', indicator: 'ema', params: { length: 21 }, timeframe: '5m' }],
  long_entry: { type: 'compare', left: { kind: 'indicator', ref: 'f' }, op: 'crosses_above', right: { kind: 'indicator', ref: 's' }, within: 1 }, short_entry: null, long_exit: null, short_exit: null,
  sizing: { method: 'percent_equity', value: 10 }, stop: { mode: 'none', value: 0, atr_length: 14 }, target: { mode: 'none', value: 0, atr_length: 14 }, trail: { mode: 'none', value: 0, atr_length: 14 },
  session: { no_entry_after: null, square_off: null }, constraints: { allow_long: true, allow_short: false, max_trades_per_day: null }, python: null, source: { type: 'visual', artifact: null, provenance: {} },
};
const DETAIL = { ...INTRADAY, versions: [{ version: 2, created_at: '2025-01-02T00:00:00Z', source_type: 'visual', definition: DEF, params: {}, definition_hash: 'abcdef123456', locked: true, has_source: false, source_filename: null },
  { version: 1, created_at: '2025-01-01T00:00:00Z', source_type: 'visual', definition: DEF, params: {}, definition_hash: '111111111111', locked: false, has_source: false, source_filename: null }],
  tunable_parameters: [{ key: 'f.length', value: 9, kind: 'int', min: 1, max: 500 }] };
const STARTER = { ...INTRADAY, id: 'st1', name: 'Starter: EMA + RSI + VWAP Confirmation', description: 'EMA 9/21 crossover confirmed by RSI, VWAP and volume.', status: 'suspended', current_version: 1,
  source_type: 'starter', origin: 'starter', origin_label: 'Built-in Starter Strategy', starter_key: 'ema_rsi_vwap', derived_from: null, tested: false, last_backtest: null,
  indicator_summary: ['EMA(9)@5m', 'EMA(21)@5m', 'RSI(14)@5m'] };
const STARTER_DEF = { ...DEF, name: STARTER.name, universe: [], source: { type: 'starter', artifact: null, provenance: { starter_key: 'ema_rsi_vwap' } },
  long_entry: { type: 'group', op: 'and', children: [{ type: 'compare', left: { kind: 'indicator', ref: 'f' }, op: 'crosses_above', right: { kind: 'indicator', ref: 's' }, within: 1 },
    { type: 'compare', left: { kind: 'price', field: 'close', timeframe: '5m' }, op: '>', right: { kind: 'price', field: 'close', timeframe: '15m' }, within: 1 }] } };
const OPT = { ...INTRADAY, id: 'op1', name: 'NIFTY Momentum Options Scalper', description: 'One-minute options-buying strategy using five-minute underlying trend confirmation.', status: 'suspended', current_version: 1,
  source_type: 'starter', origin: 'starter', origin_label: 'Built-in Starter Strategy', starter_key: 'nifty_options_scalper', derived_from: null, tested: false, last_backtest: null, strategy_type: 'options_buying', decision_timeframe: '1m',
  indicator_summary: ['VWAP()@5m', 'EMA(9)@5m'], options_summary: { market: 'NIFTY options', type: 'Options buying', directions: ['CE', 'PE'], base_data: '1-minute', trend_timeframe: '5m', entry_timeframe: '1m', max_duration_minutes: 5, premium_stop_pct: 12, target_r: 1.5, price_mode: 'quote_accurate' },
  data_coverage: { mode: 'unavailable', ready: false, blockers: ['No NIFTY futures 1-minute data', 'No option contract master'], datasets: {} } };
const OPT_DEF = { ...DEF, name: OPT.name, universe: [], decision_timeframe: '1m', source: { type: 'starter', artifact: null, provenance: { starter_key: 'nifty_options_scalper' } },
  options: { strategy_type: 'options_buying',
    underlying: { underlying: 'NIFTY', signal_source: 'futures', spot_instrument_id: null, trend_timeframe: '5m', entry_timeframe: '1m', ema_fast: 9, ema_slow: 21, ema_pullback_alt: 20, ema_slope_bars: 3, adx_length: 14, adx_min: 18, rsi_length: 14, rsi_long_min: 52, rsi_short_max: 48, atr_length: 14, pullback_atr: 0.25, pullback_lookback: 3, volume_length: 20, volume_multiple: 1.2, max_candle_range_atr: 1.5, max_vwap_extension_atr: 3, swing_left: 3, swing_right: 3, start_time: '09:30', last_entry_time: '14:45' },
    selection: { allow_ce: true, allow_pe: true, strike_choices: ['atm', 'itm1'], delta_min: 0.45, delta_max: 0.6, delta_preferred: 0.55, expiry_rank: 0, allow_expiry_day: false, max_spread_pct: 0.75, quote_max_age_seconds: 15, min_volume: 0, min_depth_lots: 1, allow_calculated_greeks: true, risk_free_rate_pct: 6.5 },
    execution: { price_mode: 'quote_accurate', premium_stop_pct: 12, target_r: 1.5, trailing_activation_r: null, trailing_distance_pct: null, max_hold_minutes: 5, underlying_invalidation: true, opposite_signal_exit: true, thesis_exit: true, slippage_ticks: 0, tick_size: 0.05, entry_latency_seconds: 1, max_entry_wait_seconds: 30, ohlc_half_spread_pct: 0.5, fee_schedule: 'dhan_nse_options_2026_11' },
    risk: { risk_per_trade_pct: 0.25, daily_loss_limit_pct: 0.75, max_positions: 1, sizing_method: 'risk_percent', fixed_lots: 1 }, event_blackouts: [] } };
const COVERAGE = { underlying: 'NIFTY', as_of: '2026-11-05T10:00:00Z', backtest_mode: 'unavailable', backtest_ready: false,
  blockers: ['No NIFTY futures 1-minute data: the underlying signal (traded price and volume) cannot be evaluated.', 'No option contract master: expiries, strikes and lot sizes are unknown, so no contract can be identified.'],
  datasets: { futures_1m: { label: 'NIFTY futures 1-minute OHLCV', provider: null, status: 'missing', rows: 0 }, contract_master: { label: 'Effective-dated option contract master (expiry, strike, lot size)', provider: null, status: 'missing', rows: 0 } },
  live: { option_chain_api: 'Dhan /v2/optionchain (live snapshot only)', dhan_broker_credentials_present: true, stored_quote_freshness_seconds: null, live_ready: false, note: 'Live chain requires a stored NIFTY underlying instrument.' },
  calendar: { nse_calendar_years_configured: [2026], note: 'x' }, disclaimer: 'Coverage is measured from stored rows. Missing datasets are never substituted.' };
const INSTRUMENTS = [{ id: 'i1', symbol: 'DASH', name: 'Dash Ltd', market: 'equity_in', currency: 'INR', bars_1m: 1875, first_bar: '2025-01-06T03:45:00Z', last_bar: '2025-01-10T10:00:00Z' }];
const HEALTH = {
  counts: { total: 3, active: 1, suspended: 1, retired: 1 }, recent_signals: [], recent_backtests: [],
  data_health: [{ instrument_id: 'i1', symbol: 'DASH', market: 'equity_in', status: 'warn', bars: 1800, last_bar_age_seconds: 600, missing_pct: 1.2, issues: ['22 missing 1-minute candle(s)'] }],
};
const trade = (n: number, net: string) => ({
  instrument_id: 'i1', symbol: 'DASH', side: 'long', qty: '10', signal_time: '2025-01-06T04:00:00Z', entry_time: `2025-01-0${n}T04:00:00Z`, exit_time: `2025-01-0${n}T04:20:00Z`, entry_raw: '100', entry_fill: '100.10',
  exit_raw: '101', exit_fill: '100.95', exit_reason: 'target', stop_initial: '99.00', target: '101.00', gross_pnl: '10.00', slippage_cost: '2.00', charges: '3.00', net_pnl: net, r_multiple: '0.5',
  cost_breakdown: { schedule_id: 'dhan_nse_intraday_2026_11', buy_turnover: '100000', sell_turnover: '101000', turnover: '201000', brokerage: '40.00', stt: '25', exchange_txn: '6.17', sebi: '0.20', ipft: '0.00', stamp_duty: '3', gst: '8.35', dp_charges: '0', other: '0', total: '82.72', pct_of_turnover: '0.0412', orders: [{ order_id: 'entry', turnover: '100000', brokerage: '20.00' }, { order_id: 'exit', turnover: '101000', brokerage: '20.00' }] },
  holding_minutes: 20, ambiguous_stop_and_target_same_bar: n === 2, explain: [{ label: 'EMA(9)@5m crosses above EMA(21)@5m', satisfied: true, depth: 0, kind: 'compare', left: 1.5, right: 1.4 }],
});
const RESULT = {
  metrics: { total_trades: 2, winning_trades: 1, losing_trades: 1, win_rate: 0.5, net_profit: '1234.50', return_pct: 0.123, profit_factor_net: 1.8, expectancy_per_trade: '617.25', max_drawdown_pct: -0.4, avg_holding_minutes: 20,
    gross_pnl_before_costs: '1300.00', total_slippage: '40.00', total_charges: '25.50', total_buy_turnover: '200000.00', total_sell_turnover: '202000.00', total_turnover: '402000.00', charges_pct_of_turnover: 0.0412,
    cost_components: { brokerage: '80.00', stt: '50.00', exchange_txn: '12.34', sebi: '0.40', ipft: '0.00', stamp_duty: '6.00', gst: '16.70', dp_charges: '0.00', other: '0.00' }, sharpe_ratio: null, sortino_ratio: null, ratios_note: 'needs >= 30 trading days and >= 30 trades' },
  equity_curve: [{ t: '2025-01-06T04:00:00Z', equity: 1000000, drawdown_pct: 0 }, { t: '2025-01-07T04:00:00Z', equity: 1001234.5, drawdown_pct: -0.1 }],
  monthly: [{ month: '2025-01', net_pnl: '1234.50', return_pct_of_initial: 0.12 }], by_side: { long: { trades: 2, net_pnl: '1234.50', win_rate: 0.5, expectancy: '617.25' } }, by_instrument: {}, by_session: {},
  exit_reasons: { target: 2 }, skipped_signals: { max_concurrent_positions: 1 }, trade_distribution: null,
  benchmark: { label: 'Buy & hold reference', per_instrument: [], average_return_pct: 1.2 },
  trades: [trade(6, '700.00'), trade(2, '-120.00')], warnings: ['only 2 trade(s): fewer than 30 is too small a sample for the statistics to be reliable'], sample: { trades: 2, reliable: false },
  assumptions: { entry_fill: 'next base bar open after the signal bar closes', same_bar_stop_and_target: 'stop assumed first' }, versions: { engine: 'intraday-engine/1.0.0', indicator_library: '1.0.0' },
  dataset: { sha256: 'a'.repeat(64), instruments: [{ symbol: 'DASH', bars: 1875, start: '2025-01-06T03:45', end: '2025-01-10T10:00', quality: { status: 'ok', issues: [] } }] },
};
const RUN = (status: string) => ({ id: 'r1', strategy_id: 's1', strategy_version: 2, kind: 'single', status, progress: status === 'completed' ? 100 : 10, created_at: '2025-01-11T00:00:00Z', finished_at: null, error: null,
  engine_version: 'intraday-engine/1.0.0', indicator_version: '1.0.0', definition_hash: 'abcdef1234567890', config: { slippage_bps: '2' }, metrics: status === 'completed' ? RESULT.metrics : null, headline: {}, result: status === 'completed' ? RESULT : undefined });
const SIGNAL = {
  id: 'g1', strategy_id: 's1', strategy_name: 'EMA cross', strategy_version: 2, instrument_id: 'i1', symbol: 'DASH', market: 'equity_in', direction: 'long', status: 'new', suppression_reason: null, timeframe: '5m',
  signal_time: '2025-01-10T09:55:00Z', reference_price: '101.50', currency: 'INR', indicators: { f: { value: 101.2 } }, conditions_met: [{ label: 'EMA(9)@5m crosses above EMA(21)@5m', satisfied: true, depth: 0, kind: 'compare', left: 101.2, right: 101.1 }],
  conditions_unmet: [{ label: 'RSI(14)@15m > 55', satisfied: false, depth: 0, kind: 'compare', left: 52, right: 55 }], rationale: 'LONG on the 5m candle', data_freshness: { age_minutes: 1.2, state: 'fresh', stale_after_minutes: 10, last_bar_close: '2025-01-10T09:56:00Z', bars_used: 800, quality_status: 'ok' }, expires_at: '2025-01-10T10:10:00Z', created_at: '2025-01-10T09:56:00Z',
};

function install(extra: Record<string, (body: unknown) => unknown> = {}) {
  overrides = extra;
  api.mockImplementation(async (path: string, init?: RequestInit) => {
    const method = (init?.method ?? 'GET').toUpperCase();
    const body = typeof init?.body === 'string' && init.body ? JSON.parse(init.body) : undefined;
    calls.push({ path, method, body });
    const key = `${method} ${path}`;
    for (const [pattern, fn] of Object.entries(overrides)) if (key === pattern || (pattern.endsWith('*') && key.startsWith(pattern.slice(0, -1)))) return fn(body);
    if (key === 'GET /signals-lab/meta') return META;
    if (key === 'GET /signals-lab/strategies') return [INTRADAY, LEGACY, SUSPENDED, STARTER, OPT];
    if (key === 'GET /signals-lab/strategies/op1') return { ...OPT, versions: [{ version: 1, created_at: '2025-01-01T00:00:00Z', source_type: 'starter', definition: OPT_DEF, params: {}, definition_hash: 'beef', locked: false, has_source: false, source_filename: null }] };
    if (key.startsWith('GET /signals-lab/options/coverage')) return COVERAGE;
    if (key === 'GET /signals-lab/strategies/st1') return { ...STARTER, versions: [{ version: 1, created_at: '2025-01-01T00:00:00Z', source_type: 'starter', definition: STARTER_DEF, params: {}, definition_hash: 'cafe', locked: false, has_source: false, source_filename: null }] };
    if (key === 'GET /signals-lab/starters') return { readiness: { equity_in: { instruments_with_1m_data: 9, backtest_ready: true } } };
    if (key === 'POST /signals-lab/starters/install') return { installed: 0, skipped: [], total: 8 };
    if (key === 'GET /signals-lab/strategies/s1') return DETAIL;
    if (key.startsWith('GET /signals-lab/instruments')) return INSTRUMENTS;
    if (key === 'GET /signals-lab/health') return HEALTH;
    if (key === 'POST /signals-lab/validate') return { valid: true, issues: [], definition_hash: 'f'.repeat(64), requirements: { timeframes: ['5m'], indicators: [], warmup_base_minutes: 105, condition_nodes: 1 } };
    if (key.startsWith('GET /signals-lab/backtests?')) return [];
    if (key.startsWith('GET /signals-lab/data-quality/')) return { status: 'ok', bars: 9000, missing_pct: 0.4, issues: [] };
    if (key.startsWith('GET /signals-lab/signals')) return [SIGNAL];
    throw new Error(`unmocked ${key}`);
  });
}

async function mount() {
  render(<SignalsWorkspace />);
  await screen.findByRole('button', { name: 'Dashboard' });
  await waitFor(() => expect(screen.getByLabelText('Strategy counts')).toBeInTheDocument());
}
// A new blank strategy opens in the guided steps; most tests here exercise the full editor.
const advancedEditor = () => { const b = screen.queryByRole('button', { name: 'Advanced editor' }); if (b) fireEvent.click(b); };
// Two levels: a primary section, then the tab inside it. `tab('Test one strategy')` opens Backtest and then Backtesting.
const PRIMARY: Record<string, string> = { 'Strategy Dashboard': 'Dashboard', 'Strategy Library': 'Strategies', 'Strategy Registry': 'Strategies', 'Strategy Builder': 'Strategies', 'Indicator Library': 'Strategies', 'Import & Validation': 'Strategies',
  'Compare strategies': 'Backtest', 'Test one strategy': 'Backtest', 'Forward Test': 'Forward test', 'Live Signals': 'Live signals', 'Jobs & scanner': 'Jobs' };
const tab = (name: string) => {
  fireEvent.click(screen.getByRole('button', { name: PRIMARY[name] }));
  const secondary = screen.queryByRole('navigation', { name: `${PRIMARY[name]} sections` });
  if (secondary) fireEvent.click(within(secondary).getByRole('button', { name }));
};

describe('Signals workspace', () => {
  beforeEach(() => { calls = []; install(); });
  afterEach(() => { cleanup(); vi.clearAllMocks(); });

  it('shows every section and the dashboard with strategy counts and data-quality warnings', async () => {
    await mount();
    for (const name of ['Dashboard', 'Strategies', 'Backtest', 'Forward test', 'Live signals', 'Jobs']) expect(screen.getByRole('button', { name })).toBeInTheDocument();
    for (const name of Object.keys(PRIMARY)) { tab(name); expect(screen.getByRole('button', { name: PRIMARY[name] }).getAttribute('aria-current')).toBe('page'); }   // every old tab is still reachable
    tab('Strategy Dashboard');
    const counts = within(screen.getByLabelText('Strategy counts'));
    expect(counts.getByText('Total strategies').nextSibling).toHaveTextContent('3');
    expect(screen.getByText(/1 instrument\(s\) have 1-minute data problems/)).toBeInTheDocument();
    expect(screen.getByText('22 missing 1-minute candle(s)')).toBeInTheDocument();
  });

  it('is a strategy workbench: the VAR research, derivatives data and legacy daily pages are no longer in the navigation', async () => {
    await mount();
    const nav = within(screen.getByRole('navigation', { name: 'Signals sections' })).getAllByRole('button').map((b) => b.textContent);
    expect(nav).toEqual(['Dashboard', 'Strategies', 'Backtest', 'Forward test', 'Live signals', 'Jobs']);
    fireEvent.click(screen.getByRole('button', { name: 'Strategies' }));
    expect(within(screen.getByRole('navigation', { name: 'Strategies sections' })).getAllByRole('button').map((b) => b.textContent)).toEqual(['Strategy Library', 'Strategy Registry', 'Strategy Builder', 'Indicator Library', 'Import & Validation']);
    for (const gone of ['Legacy daily research', 'Derivatives Data', 'Intraday research (VAR)']) expect(screen.queryByRole('button', { name: gone })).toBeNull();
  });

  it('library lists intraday and legacy strategies with all actions and filters', async () => {
    await mount();
    tab('Strategy Library');
    await screen.findByText('Reliance MA');
    expect(screen.getByText('EMA cross')).toBeInTheDocument();
    expect(screen.getByText('test2')).toBeInTheDocument();
    expect(screen.getAllByText('legacy daily').length).toBe(2);
    for (const a of ['Open', 'Edit as new version', 'Duplicate', 'Suspend', 'Retire', 'Backtest']) expect(screen.getAllByRole('button', { name: a }).length).toBeGreaterThan(0);
    expect(screen.getAllByRole('button', { name: 'Convert to intraday draft' }).length).toBe(2);
    fireEvent.change(screen.getByLabelText('Status'), { target: { value: 'retired' } });
    expect(screen.getByText('Reliance MA')).toBeInTheDocument();
    expect(screen.queryByText('EMA cross')).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Status'), { target: { value: 'all' } });
    fireEvent.change(screen.getByLabelText('Type'), { target: { value: 'intraday' } });
    expect(screen.queryByText('Reliance MA')).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Type'), { target: { value: 'all' } });
    fireEvent.change(screen.getByLabelText('Search strategies'), { target: { value: 'test' } });
    expect(screen.getByText('test2')).toBeInTheDocument();
    expect(screen.queryByText('EMA cross')).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Search strategies'), { target: { value: 'zzz' } });
    expect(screen.getByText('No strategies match these filters')).toBeInTheDocument();
  });

  it('retire asks for confirmation first and only then calls the API', async () => {
    install({ 'POST /strategies/s1/retire': () => ({}) });
    await mount();
    tab('Strategy Library');
    await screen.findByText('EMA cross');
    const row = screen.getByText('EMA cross').closest('tr') as HTMLElement;
    fireEvent.click(within(row).getByRole('button', { name: 'Retire' }));
    const dialog = screen.getByRole('dialog');
    expect(dialog).toHaveTextContent(/Retiring is permanent/);
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
    expect(calls.some((c) => c.path.endsWith('/retire'))).toBe(false);
    fireEvent.click(within(row).getByRole('button', { name: 'Retire' }));
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Retire' }));
    await waitFor(() => expect(calls.some((c) => c.path === '/strategies/s1/retire' && c.method === 'POST')).toBe(true));
  });

  it('opening a strategy shows version history with hashes and lock state', async () => {
    await mount();
    tab('Strategy Library');
    fireEvent.click((await screen.findAllByRole('button', { name: 'Open' }))[0]);
    const hist = await screen.findByLabelText('EMA cross history');
    expect(within(hist).getByText('v2 (current)')).toBeInTheDocument();
    expect(within(hist).getByText('abcdef123456')).toBeInTheDocument();
    expect(within(hist).getAllByText('yes').length).toBe(1);
  });

  it('builder creates a strategy: nested AND/OR conditions, validation, and save payload', async () => {
    install({ 'POST /signals-lab/strategies': (b) => ({ ...INTRADAY, id: 'new', name: (b as { definition: { name: string } }).definition.name, current_version: 1 }) });
    await mount();
    tab('Strategy Builder');
    advancedEditor();
    fireEvent.change(await screen.findByPlaceholderText(/EMA 20\/50/), { target: { value: 'My cross' } });
    fireEvent.click(await screen.findByRole('button', { name: 'Add DASH' }));
    fireEvent.click(screen.getByRole('button', { name: /Indicators \(0\)/ }));
    fireEvent.change(screen.getByLabelText('Indicator to add'), { target: { value: 'ema' } });
    fireEvent.click(screen.getByRole('button', { name: 'Add indicator' }));
    fireEvent.click(screen.getByRole('button', { name: 'Add indicator' }));
    expect(screen.getByLabelText('ema length')).toBeInTheDocument();
    expect(screen.getByLabelText('ema_2 length')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Entry & exit logic' }));
    const longEntry = screen.getByLabelText('Long entry');
    fireEvent.click(within(longEntry).getByRole('button', { name: '+ AND / OR group' }));
    const group = within(longEntry).getByRole('group', { name: 'AND group' });
    fireEvent.click(within(group).getByRole('button', { name: '+ Group' }));
    expect(within(longEntry).getByRole('group', { name: 'OR group' })).toBeInTheDocument();      // nested OR inside AND
    fireEvent.click(within(group).getAllByRole('button', { name: '+ Time window' })[0]);   // outer group's button
    expect(within(group).getByLabelText('Window start')).toBeInTheDocument();
    fireEvent.change(within(longEntry).getAllByLabelText('Comparison')[0], { target: { value: 'crosses_above' } });
    await waitFor(() => expect(calls.some((c) => c.path === '/signals-lab/validate')).toBe(true));
    const save = await screen.findByRole('button', { name: 'Save strategy' });
    await waitFor(() => expect(save).toBeEnabled());
    fireEvent.click(save);
    await waitFor(() => expect(calls.some((c) => c.path === '/signals-lab/strategies' && c.method === 'POST')).toBe(true));
    const body = calls.find((c) => c.path === '/signals-lab/strategies' && c.method === 'POST')!.body as { definition: { name: string; universe: string[]; indicators: unknown[]; long_entry: { type: string; op: string; children: { type: string }[] }; schema_version: string } };
    expect(body.definition.name).toBe('My cross');
    expect(body.definition.universe).toEqual(['i1']);
    expect(body.definition.indicators).toHaveLength(2);
    expect(body.definition.long_entry.type).toBe('group');
    expect(body.definition.long_entry.children.map((c) => c.type)).toEqual(['compare', 'group', 'time_window']);
    expect(body.definition.schema_version).toBe('intraday-strategy/1');
    expect(await screen.findByText(/Created My cross/)).toBeInTheDocument();
  });

  it('builder surfaces validation errors and blocks saving', async () => {
    install({ 'POST /signals-lab/validate': () => ({ valid: false, issues: [{ level: 'error', message: 'unknown indicator alias ghost', where: 'long_entry' }], requirements: null }) });
    await mount();
    tab('Strategy Builder');
    advancedEditor();
    fireEvent.change(await screen.findByPlaceholderText(/EMA 20\/50/), { target: { value: 'Bad' } });
    fireEvent.click(await screen.findByRole('button', { name: 'Add DASH' }));
    await waitFor(() => expect(screen.getByRole('button', { name: /Validation · 1/ })).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: /Validation · 1/ }));
    expect((await screen.findAllByText(/unknown indicator alias ghost/)).length).toBeGreaterThan(0);
    expect(screen.getByRole('button', { name: 'Save strategy' })).toBeDisabled();
    expect(screen.getByText(/^To save:/).textContent).toContain('unknown indicator alias ghost');       // the reason is said next to the button
  });

  it('editing an existing strategy saves a NEW version and says earlier versions are untouched', async () => {
    install({ 'POST /signals-lab/strategies/s1/versions': () => ({ ...INTRADAY, current_version: 3 }) });
    await mount();
    tab('Strategy Library');
    const row = (await screen.findByText('EMA cross')).closest('tr') as HTMLElement;
    fireEvent.click(within(row).getByRole('button', { name: 'Edit as new version' }));
    expect(await screen.findByText('Editing EMA cross')).toBeInTheDocument();
    expect(screen.getByText(/version 2 stays exactly as it is/)).toBeInTheDocument();
    const save = await screen.findByRole('button', { name: 'Save as version 3' });
    await waitFor(() => expect(save).toBeEnabled());
    fireEvent.click(save);
    await waitFor(() => expect(calls.some((c) => c.path === '/signals-lab/strategies/s1/versions' && c.method === 'POST')).toBe(true));
    expect(await screen.findByText(/Earlier versions are unchanged/)).toBeInTheDocument();
  });

  it('legacy strategies convert to an unsaved intraday draft instead of being edited', async () => {
    install({ 'GET /signals-lab/strategies/l1/convert-draft*': () => ({ definition: { ...DEF, name: 'Reliance MA (intraday)', source: { type: 'legacy', artifact: null, provenance: {} } }, note: 'not equivalent' }) });
    await mount();
    tab('Strategy Library');
    const row = (await screen.findByText('Reliance MA')).closest('tr') as HTMLElement;
    fireEvent.click(within(row).getByRole('button', { name: 'Convert to intraday draft' }));
    expect(await screen.findByText('not equivalent')).toBeInTheDocument();
    expect(screen.getByDisplayValue('Reliance MA (intraday)')).toBeInTheDocument();
    expect(calls.filter((c) => c.method === 'POST' && c.path.includes('/strategies')).length).toBe(0);
  });

  it('indicator library is searchable, filterable and shows formulas', async () => {
    await mount();
    tab('Indicator Library');
    expect(await screen.findByText('Exponential Moving Average')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'momentum' }));
    expect(screen.queryByText('Exponential Moving Average')).not.toBeInTheDocument();
    expect(screen.getByText('MACD')).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Search indicators'), { target: { value: 'relative' } });
    fireEvent.click(screen.getByRole('button', { name: 'Relative Strength Index' }));
    expect(screen.getByText(/Relative Strength Index formula/)).toBeInTheDocument();
  });
});

describe('Built-in starter strategies', () => {
  beforeEach(() => { calls = []; install(); });
  afterEach(() => { cleanup(); vi.clearAllMocks(); });

  it('are shown (not auto-installed on load), labelled, described, untested and offer Open in Builder / Customize instead of editing in place', async () => {
    await mount();
    expect(calls.some((c) => c.path === '/signals-lab/starters/install')).toBe(false);   // deleted strategies must not silently come back
    tab('Strategy Library');
    const row = (await screen.findByText('Starter: EMA + RSI + VWAP Confirmation')).closest('tr') as HTMLElement;
    expect(within(row).getByText('Built-in Starter Strategy')).toBeInTheDocument();
    expect(within(row).getByText('not tested')).toBeInTheDocument();
    expect(within(row).getByText(/EMA 9\/21 crossover confirmed by RSI/)).toBeInTheDocument();
    expect(within(row).getByText(/EMA\(9\)@5m · EMA\(21\)@5m · RSI\(14\)@5m/)).toBeInTheDocument();
    expect(within(row).getByText('v1')).toBeInTheDocument();
    expect(within(row).getByRole('button', { name: 'Open in Builder' })).toBeInTheDocument();
    expect(within(row).getByRole('button', { name: 'Customize' })).toBeInTheDocument();
    expect(within(row).queryByRole('button', { name: 'Edit as new version' })).not.toBeInTheDocument();
    expect(screen.getByText(/have not been tested|not been tested/)).toBeInTheDocument();
    expect(screen.getByText(/Indian equity 9 instrument\(s\) with 1m data/)).toBeInTheDocument();
    expect(screen.queryByText(/crypto/i)).not.toBeInTheDocument(); // crypto lives on its own page
    fireEvent.change(screen.getByLabelText('Source'), { target: { value: 'starter' } });
    expect(screen.queryByText('EMA cross')).not.toBeInTheDocument();
    fireEvent.click(within(row).getByRole('button', { name: 'Open' }));
    expect(await screen.findByText(/evaluation timeframe 5m .* not tested/)).toBeInTheDocument();
  });

  it('Open in Builder loads an editable copy, re-times with the evaluation timeframe, and saves a NEW strategy with lineage', async () => {
    install({ 'POST /signals-lab/strategies': (b) => ({ ...INTRADAY, id: 'copy', name: (b as { definition: { name: string } }).definition.name, current_version: 1 }) });
    await mount();
    tab('Strategy Library');
    const row = (await screen.findByText('Starter: EMA + RSI + VWAP Confirmation')).closest('tr') as HTMLElement;
    fireEvent.click(within(row).getByRole('button', { name: 'Open in Builder' }));
    expect(await screen.findByDisplayValue('EMA + RSI + VWAP Confirmation (custom)')).toBeInTheDocument();
    expect(screen.getByText(/Customizing a copy of/)).toBeInTheDocument();
    expect(screen.getByText(/the original and its versions are unchanged/i)).toBeInTheDocument();
    // everything is editable: indicators, params and conditions are not read-only
    fireEvent.click(screen.getByRole('button', { name: /Indicators \(2\)/ }));
    const len = screen.getByLabelText('f length') as HTMLInputElement;
    expect(len).toBeEnabled();
    fireEvent.change(len, { target: { value: '12' } });
    fireEvent.change(screen.getByLabelText('Decision timeframe'), { target: { value: '15m' } });
    expect((screen.getByLabelText('f timeframe') as HTMLSelectElement).value).toBe('15m');
    fireEvent.click(screen.getByRole('button', { name: 'Entry & exit logic' }));
    expect(screen.getByLabelText('Long entry')).toBeInTheDocument();
    fireEvent.click(await screen.findByRole('button', { name: 'Add DASH' }));
    const save = await screen.findByRole('button', { name: 'Save strategy' });
    await waitFor(() => expect(save).toBeEnabled());
    fireEvent.click(save);
    await waitFor(() => expect(calls.some((c) => c.path === '/signals-lab/strategies' && c.method === 'POST')).toBe(true));
    const def = (calls.find((c) => c.path === '/signals-lab/strategies' && c.method === 'POST')!.body as { definition: { name: string; decision_timeframe: string; universe: string[]; indicators: { params: { length: number }; timeframe: string }[]; source: { type: string; provenance: { derived_from: { strategy_id: string; version: number; starter_key: string } } }; long_entry: { children: { right: { timeframe?: string }; left: { timeframe?: string } }[] } } }).definition;
    expect(def.name).toBe('EMA + RSI + VWAP Confirmation (custom)');
    expect(def.decision_timeframe).toBe('15m');
    expect(def.indicators[0]).toMatchObject({ timeframe: '15m', params: { length: 12 } });
    expect(def.long_entry.children[1].left.timeframe).toBe('15m');      // follows the evaluation timeframe
    expect(def.long_entry.children[1].right.timeframe).toBe('15m');      // (was 15m deliberately; stays 15m)
    expect(def.source.type).toBe('visual');
    expect(def.source.provenance.derived_from).toEqual({ strategy_id: 'st1', version: 1, starter_key: 'ema_rsi_vwap', name: 'Starter: EMA + RSI + VWAP Confirmation' });
    expect(calls.some((c) => c.path.includes('/strategies/st1/versions'))).toBe(false);   // the starter was not modified
  });

  it('keeps deliberate higher-timeframe confirmations when the evaluation timeframe changes', async () => {
    await mount();
    tab('Strategy Library');
    const row = (await screen.findByText('Starter: EMA + RSI + VWAP Confirmation')).closest('tr') as HTMLElement;
    fireEvent.click(within(row).getByRole('button', { name: 'Open in Builder' }));
    await screen.findByDisplayValue('EMA + RSI + VWAP Confirmation (custom)');
    fireEvent.change(screen.getByLabelText('Decision timeframe'), { target: { value: '1m' } });
    fireEvent.click(screen.getByRole('button', { name: 'Validation' }));
    fireEvent.click(screen.getByRole('button', { name: 'Entry & exit logic' }));
    const sel = screen.getAllByLabelText('Right field');
    expect(sel.length).toBeGreaterThan(0);
    const tfs = screen.getAllByLabelText('Right timeframe') as HTMLSelectElement[];
    expect(tfs.map((x) => x.value)).toContain('15m');
  });

  it('Customize saves an independent copy through the duplicate endpoint', async () => {
    install({ 'POST /signals-lab/strategies/st1/duplicate': () => ({ ...STARTER, id: 'dup', origin: 'customized' }) });
    await mount();
    tab('Strategy Library');
    const row = (await screen.findByText('Starter: EMA + RSI + VWAP Confirmation')).closest('tr') as HTMLElement;
    fireEvent.click(within(row).getByRole('button', { name: 'Customize' }));
    fireEvent.change(screen.getByDisplayValue('EMA + RSI + VWAP Confirmation (custom)'), { target: { value: 'My variant' } });
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Duplicate' }));
    await waitFor(() => expect(calls.some((c) => c.path === '/signals-lab/strategies/st1/duplicate' && (c.body as { name: string }).name === 'My variant')).toBe(true));
  });
});

describe('NIFTY options strategy', () => {
  beforeEach(() => { calls = []; install(); });
  afterEach(() => { cleanup(); vi.clearAllMocks(); });

  it('shows in the Library with its options summary, data coverage status and an untested state, without fabricated results', async () => {
    await mount();
    tab('Strategy Library');
    const row = (await screen.findByText('NIFTY Momentum Options Scalper')).closest('tr') as HTMLElement;
    const summary = within(row).getByLabelText('Options summary');
    expect(summary).toHaveTextContent('NIFTY options · Options buying · CE/PE · base 1-minute · trend 5m · max 5 min');
    expect(within(row).getByText('data coverage: unavailable')).toBeInTheDocument();
    expect(within(row).getByText('not tested · data unavailable')).toBeInTheDocument();
    expect(within(row).getByText('Built-in Starter Strategy')).toBeInTheDocument();
    for (const a of ['Open in Builder', 'Customize', 'Duplicate', 'Backtest']) expect(within(row).queryAllByRole('button', { name: a }).length >= 0).toBe(true);
    expect(within(row).getByRole('button', { name: 'Open in Builder' })).toBeInTheDocument();
    expect(within(row).getByRole('button', { name: 'Backtest' })).toBeInTheDocument();
    expect(within(row).queryByText(/win rate|profit/i)).not.toBeInTheDocument();
    fireEvent.click(within(row).getByRole('button', { name: 'Open' }));
    expect(await screen.findByText(/Options backtesting unavailable: No NIFTY futures 1-minute data/)).toBeInTheDocument();
  });

  it('opens in the Builder with the four option groups, every parameter editable, regenerates rules and saves a new strategy', async () => {
    install({
      'POST /signals-lab/options/compile': () => ({ indicators: [{ alias: 't_fast', indicator: 'ema', params: { length: 8 }, timeframe: '5m' }], long_entry: DEF.long_entry, short_entry: null, long_exit: null, short_exit: null }),
      'POST /signals-lab/strategies': (b) => ({ ...OPT, id: 'mine', name: (b as { definition: { name: string } }).definition.name, origin: 'customized', current_version: 1 }),
    });
    await mount();
    tab('Strategy Library');
    const row = (await screen.findByText('NIFTY Momentum Options Scalper')).closest('tr') as HTMLElement;
    fireEvent.click(within(row).getByRole('button', { name: 'Open in Builder' }));
    expect(await screen.findByDisplayValue('NIFTY Momentum Options Scalper (custom)')).toBeInTheDocument();
    const panel = within(await screen.findByLabelText('Options strategy configuration'));
    for (const legend of ['1 · Underlying signal', '2 · Contract selection', '3 · Execution & exits', '4 · Risk management']) expect(panel.getByText(legend)).toBeInTheDocument();
    // underlying signal
    for (const label of ['EMA fast', 'EMA slow', 'ADX min', 'RSI long >', 'RSI short <', 'Pullback ×ATR', 'Volume ×avg', 'Max candle ×ATR', 'Max VWAP ext ×ATR']) expect(panel.getByLabelText(label)).toBeEnabled();
    fireEvent.change(panel.getByLabelText('EMA fast'), { target: { value: '8' } });
    fireEvent.change(panel.getByLabelText('ADX min'), { target: { value: '22' } });
    // contract selection
    for (const label of ['Min |delta|', 'Max |delta|', 'Preferred |delta|', 'Max spread % of mid', 'Quote max age (s)', 'Min depth (lots)']) expect(panel.getByLabelText(label)).toBeEnabled();
    fireEvent.change(panel.getByLabelText('Preferred |delta|'), { target: { value: '0.5' } });
    fireEvent.click(panel.getByLabelText('Allow expiry day'));
    fireEvent.click(panel.getByLabelText('ITM 1'));
    // execution & exits
    for (const label of ['Premium stop %', 'Target (R multiple)', 'Max hold (min)', 'Trail activation (R)', 'Trail distance %', 'Slippage (ticks)']) expect(panel.getByLabelText(label)).toBeEnabled();
    fireEvent.change(panel.getByLabelText('Premium stop %'), { target: { value: '10' } });
    fireEvent.change(panel.getByLabelText('Target (R multiple)'), { target: { value: '2' } });
    fireEvent.change(panel.getByLabelText('Trail activation (R)'), { target: { value: '1' } });
    fireEvent.change(panel.getByLabelText('Trail distance %'), { target: { value: '5' } });
    fireEvent.click(panel.getByLabelText('Exit on underlying swing break'));
    expect(screen.getByText(/target 20.00% \(2R\)/)).toBeInTheDocument();
    // risk
    fireEvent.change(panel.getByLabelText('Risk per trade %'), { target: { value: '0.5' } });
    fireEvent.change(panel.getByLabelText('Daily loss limit %'), { target: { value: '1' } });
    fireEvent.change(panel.getByLabelText('Price mode'), { target: { value: 'indicative_ohlc' } });
    fireEvent.click(panel.getByRole('button', { name: 'Regenerate rules from these settings' }));
    expect(await screen.findByText(/regenerated from these settings/)).toBeInTheDocument();
    expect(screen.getByLabelText('Decision timeframe')).toBeDisabled();
    expect(await screen.findByText(/Options data coverage|options data coverage/i)).toBeInTheDocument();
    fireEvent.click(await screen.findByRole('button', { name: 'Add DASH' }));
    const save = await screen.findByRole('button', { name: 'Save strategy' });
    await waitFor(() => expect(save).toBeEnabled());
    fireEvent.click(save);
    await waitFor(() => expect(calls.some((c) => c.path === '/signals-lab/strategies' && c.method === 'POST')).toBe(true));
    const compile = calls.find((c) => c.path === '/signals-lab/options/compile')!.body as { ema_fast: number; adx_min: number };
    expect(compile).toMatchObject({ ema_fast: 8, adx_min: 22 });
    const body = (calls.find((c) => c.path === '/signals-lab/strategies' && c.method === 'POST')!.body as { definition: { name: string; universe: string[]; indicators: { params: { length: number } }[]; source: { provenance: { derived_from: { starter_key: string } } };
      options: { underlying: { ema_fast: number }; selection: { delta_preferred: number; allow_expiry_day: boolean; strike_choices: string[] }; execution: { premium_stop_pct: number; target_r: number; trailing_activation_r: number; trailing_distance_pct: number; underlying_invalidation: boolean; price_mode: string }; risk: { risk_per_trade_pct: number; daily_loss_limit_pct: number } } } }).definition;
    expect(body.name).toBe('NIFTY Momentum Options Scalper (custom)');
    expect(body.options.underlying.ema_fast).toBe(8);
    expect(body.options.selection).toMatchObject({ delta_preferred: 0.5, allow_expiry_day: true, strike_choices: ['atm'] });
    expect(body.options.execution).toMatchObject({ premium_stop_pct: 10, target_r: 2, trailing_activation_r: 1, trailing_distance_pct: 5, underlying_invalidation: false, price_mode: 'indicative_ohlc' });
    expect(body.options.risk).toMatchObject({ risk_per_trade_pct: 0.5, daily_loss_limit_pct: 1 });
    expect(body.indicators[0].params.length).toBe(8);
    expect(body.source.provenance.derived_from.starter_key).toBe('nifty_options_scalper');
    expect(calls.some((c) => c.path.includes('/strategies/op1/versions'))).toBe(false);          // the starter itself is never modified
  }, 15_000);

  it('a backtest without options data is refused with the blockers shown, never run on index or futures prices', async () => {
    install({ 'POST /signals-lab/backtests': () => { throw Object.assign(new Error('Options backtesting is unavailable: No NIFTY futures 1-minute data'), { detail: { code: 'options_data_unavailable', message: 'Options backtesting is unavailable: No NIFTY futures 1-minute data | No option contract master', blockers: ['No NIFTY futures 1-minute data', 'No option contract master'] } }); } });
    await mount();
    tab('Strategy Library');
    const row = (await screen.findByText('NIFTY Momentum Options Scalper')).closest('tr') as HTMLElement;
    fireEvent.click(within(row).getByRole('button', { name: 'Backtest' }));
    expect(await screen.findByLabelText('Options data coverage')).toBeInTheDocument();
    expect(screen.getByRole('list', { name: 'Blockers' })).toHaveTextContent('No NIFTY futures 1-minute data');
    expect(screen.getByText('NIFTY futures 1-minute OHLCV')).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText('DASH ✕')).toBeInTheDocument()).catch(() => undefined);
    fireEvent.focus(screen.getByLabelText('Search instruments'));
    fireEvent.mouseDown(await screen.findByRole('option', { name: /DASH/ }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Run backtest' })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: 'Run backtest' }));
    expect(await screen.findByText(/Options backtesting is unavailable: No NIFTY futures 1-minute data \| No option contract master/)).toBeInTheDocument();
  });

  it('live signals show the selected contract with bid/ask/delta, or NO TRADE reasons instead of a stale price', async () => {
    const base = { ...SIGNAL, id: 'o1', strategy_name: 'NIFTY Momentum Options Scalper' };
    install({ 'GET /signals-lab/signals?*': () => [
      { ...base, option: { ok: true, symbol: 'NIFTY 09JAN2025 23000 CE', option_type: 'CE', strike: '23000', expiry: '2025-01-09', lot_size: 25, delta: 0.52, delta_source: 'provider', bid: '100.00', ask: '100.50', spread_pct: 0.4988, quote_time: '2025-01-07T04:45:58+00:00', entry_reference_ask: '100.50', premium_stop: '88.45', premium_target: '118.60', max_hold_minutes: 5, reference_label: 'spot index', why: 'CE 23000 expiring 2025-01-09 (nearest eligible expiry)' } },
      { ...base, id: 'o2', status: 'suppressed', suppression_reason: 'NO TRADE: stale_quote_300s', option: { ok: false, no_trade_reasons: ['stale_quote_300s', 'spread_1.20pct_exceeds_0.75'], candidates: [{ strike_choice: 'atm', symbol: 'NIFTY 23000 CE', rejected: ['stale_quote_300s'] }] } },
    ] });
    await mount();
    tab('Live Signals');
    const rows = await screen.findAllByText(/NIFTY Momentum Options Scalper v2/);
    fireEvent.click(rows[0].closest('tr') as HTMLElement);
    const card = within(await screen.findByLabelText('Option contract'));
    expect(card.getByText(/NIFTY 09JAN2025 23000 CE · CE · strike 23000 · expiry 2025-01-09 · lot 25/)).toBeInTheDocument();
    expect(card.getByText(/0.520 \(provider\)/)).toBeInTheDocument();
    expect(card.getByText(/100.00 \/ 100.50 \/ 0.4988%/)).toBeInTheDocument();
    expect(card.getByText(/88.45 \/ 118.60/)).toBeInTheDocument();
    fireEvent.click(screen.getAllByText(/NIFTY Momentum Options Scalper v2/)[1].closest('tr') as HTMLElement);
    const nt = within(await screen.findByLabelText('Option contract'));
    expect(nt.getByText(/NO TRADE: stale_quote_300s, spread_1.20pct_exceeds_0.75/)).toBeInTheDocument();
    expect(nt.getByText(/A missing or stale quote is never shown as a valid price/)).toBeInTheDocument();
  });

  it('options results are labelled by price basis and report CE/PE, delta, expiry, spread, MAE/MFE and option details per trade', async () => {
    const optTrade = { ...trade(6, '700.00'), symbol: 'NIFTY 09JAN2025 23000 CE', option: { option_type: 'CE', underlying_direction: 'long', strike: '23000', expiry: '2025-01-09', lot_size: 25, lots: 3, delta: 0.52, delta_source: 'provider', entry_bid: '100.00', entry_ask: '100.50', entry_spread_pct: 0.4988, exit_bid: '118.50', exit_ask: '118.90', exit_spread_pct: 0.3, price_source: 'ohlc_indicative', selection_why: 'CE 23000 closest to preferred delta', contract_id: 'c1', master_version: 'm1', invalidation_level: 22980, mae_pct: -3.2, mfe_pct: 18.5, gap_through_stop: false, exit_unresolved: false, config: 'stop 12% / 1.5R', reference: 'spot index' } };
    const grp = { 'CE': { trades: 1, net_pnl: '700.00', win_rate: 1, expectancy: '700.00' } };
    const report = { ce_trades: 1, pe_trades: 0, avg_entry_spread_pct: 0.4988, avg_exit_spread_pct: 0.3, avg_delta_at_entry: 0.52, avg_mae_pct: -3.2, avg_mfe_pct: 18.5, max_holding_minutes: 3.1, by_option_type: grp, by_delta_bucket: { '0.50-0.55': grp.CE ? grp['CE'] : grp }, by_expiry: { '2025-01-09': grp.CE }, by_hour_ist: { '10:00': grp.CE }, by_config: { 'stop 12% / 1.5R': grp.CE }, by_price_source: { ohlc_indicative: grp.CE },
      greeks_sources: { provider: 1, calculated: 0 }, signals: { total: 4, traded: 1, no_trade_reasons: { 'no_contract:no_quote_at_timestamp': 2, daily_loss_limit_reached: 1 } }, price_basis: 'INDICATIVE: option candle prices with an assumed half-spread; not executable quotes' };
    install({ 'GET /signals-lab/strategies/s1': () => ({ ...DETAIL, versions: [{ ...DETAIL.versions[0], definition: { ...OPT_DEF, universe: ['i1'] } }] }), 'POST /signals-lab/backtests': () => RUN('queued'),
      'GET /signals-lab/backtests/r1': () => ({ ...RUN('completed'), result: { ...RESULT, trades: [optTrade], options_report: report, versions: { engine: 'e', indicator_library: '1', options_engine: 'options-engine/1.0.0' } } }) });
    await mount();
    tab('Test one strategy');
    await waitFor(() => expect(screen.getByLabelText('Options data coverage')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: 'Run backtest' }));
    const rep = within(await screen.findByLabelText('Options performance report'));
    expect(rep.getByText(/INDICATIVE RESULT\. INDICATIVE: option candle prices/)).toBeInTheDocument();
    for (const label of ['CE trades', 'PE trades', 'Avg entry spread', 'Avg exit spread', 'Avg MAE', 'Avg MFE']) expect(rep.getByText(label)).toBeInTheDocument();
    for (const title of ['CE vs PE', 'By delta bucket', 'By expiry', 'By time of day (IST)', 'By stop / target configuration', 'By price source']) expect(rep.getByText(title)).toBeInTheDocument();
    expect(rep.getByText(/no contract:no quote at timestamp: 2/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'trades' }));
    fireEvent.click(within(await screen.findByLabelText('Trade ledger')).getAllByRole('row')[1]);
    const det = within(await screen.findByLabelText('Option trade details'));
    expect(det.getByText(/NIFTY 09JAN2025 23000 CE · CE 23000 · expiry 2025-01-09 · 3 lot\(s\) × 25/)).toBeInTheDocument();
    expect(det.getByText('INDICATIVE OHLC (estimated execution)')).toBeInTheDocument();
    expect(det.getByText(/-3.2% \/ 18.5%/)).toBeInTheDocument();
  });
});

describe('Import & validation', () => {
  beforeEach(() => { calls = []; install(); });
  afterEach(() => { cleanup(); vi.clearAllMocks(); });

  const upload = (name: string, text: string) => {
    const file = new File([text], name, { type: 'text/plain' });
    Object.defineProperty(file, 'text', { value: async () => text });
    fireEvent.change(screen.getByLabelText('Strategy file'), { target: { files: [file] } });
  };

  it('rejects wrong file types before sending anything', async () => {
    await mount();
    tab('Import & Validation');
    upload('evil.exe', 'x');
    expect(await screen.findByText(/Unsupported file type/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Analyze compatibility' })).toBeDisabled();
  });

  it('unsupported Pine is blocked with line-specific errors and no way to import', async () => {
    install({ 'POST /signals-lab/import/pine': () => ({ filename: 'a.pine', sha256: 'x', version: 5, compatibility: 'unsupported', can_import: false, equivalence_validated: false, equivalence_note: 'Not validated against TradingView.',
      items: [{ line: 2, status: 'supported', construct: 'strategy()', message: '' }], errors: [{ line: 3, message: "'var' is not supported" }, { line: 7, message: 'strategy.exit() is not supported' }], warnings: [], parameters: {}, detected: { indicators: [], entries: {} }, definition: null }) });
    await mount();
    tab('Import & Validation');
    upload('a.pine', '//@version=5\nvar x = 1');
    fireEvent.click(await screen.findByRole('button', { name: 'Analyze compatibility' }));
    const report = await screen.findByLabelText('Compatibility report');
    expect(within(report).getByText('line 3')).toBeInTheDocument();
    expect(within(report).getByText('line 7')).toBeInTheDocument();
    expect(within(report).getByText(/Not validated against TradingView/)).toBeInTheDocument();
    expect(within(report).getByText(/cannot be imported/)).toBeInTheDocument();
    expect(within(report).queryByRole('button', { name: 'Review in Strategy Builder' })).not.toBeInTheDocument();
  });

  it('partial Pine needs an explicit acknowledgement, then opens the builder with the translated draft', async () => {
    install({ 'POST /signals-lab/import/pine': () => ({ filename: 'a.pine', sha256: 'x', version: 5, compatibility: 'partial', can_import: true, equivalence_validated: false, equivalence_note: 'Not validated against TradingView.',
      items: [{ line: 8, status: 'partial', construct: 'request.security', message: 'completed candles only' }], errors: [], warnings: [{}], parameters: {}, detected: { indicators: DEF.indicators, entries: { long_entry: 1 } }, definition: { ...DEF, name: 'Pine X', source: { type: 'pine', artifact: { filename: 'a.pine' }, provenance: {} } } }) });
    await mount();
    tab('Import & Validation');
    upload('a.pine', '//@version=5\nstrategy("Pine X")');
    fireEvent.click(await screen.findByRole('button', { name: 'Analyze compatibility' }));
    const report = await screen.findByLabelText('Compatibility report');
    expect(within(report).getByText('request.security')).toBeInTheDocument();
    expect(within(report).getByLabelText('Translated strategy preview')).toHaveTextContent('Pine X');
    const go = within(report).getByRole('button', { name: 'Review in Strategy Builder' });
    expect(go).toBeDisabled();
    fireEvent.click(within(report).getByRole('checkbox'));
    expect(go).toBeEnabled();
    fireEvent.click(go);
    expect(await screen.findByDisplayValue('Pine X')).toBeInTheDocument();
    expect(screen.getByText(/Translated from Pine Script/)).toBeInTheDocument();
  });

  it('rejected Python shows the line and reason and cannot be imported', async () => {
    install({ 'POST /signals-lab/import/python': () => ({ filename: 'x.py', sha256: 'x', screen_errors: [{ line: 1, message: "import of 'os' is not allowed" }], can_import: false, definition: null, allowed_imports: ['math'], isolation: 'subprocess' }) });
    await mount();
    tab('Import & Validation');
    fireEvent.click(screen.getByRole('button', { name: 'Python' }));
    upload('x.py', 'import os');
    fireEvent.click(await screen.findByRole('button', { name: 'Analyze compatibility' }));
    const report = await screen.findByLabelText('Compatibility report');
    expect(within(report).getByText('line 1')).toBeInTheDocument();
    expect(within(report).getByText(/import of 'os' is not allowed/)).toBeInTheDocument();
    expect(within(report).queryByRole('button', { name: 'Review in Strategy Builder' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /Show template/ }));
    expect(screen.getByLabelText('Python template')).toBeInTheDocument();
    expect(screen.getByText(/no file writes, no network, no environment or secrets/)).toBeInTheDocument();
  });
});

describe('Backtesting and live signals', () => {
  beforeEach(() => { calls = []; install(); });
  afterEach(() => { cleanup(); vi.clearAllMocks(); });

  it('configures, runs and reviews a backtest: metrics, ledger, assumptions, data provenance', async () => {
    install({
      'POST /signals-lab/backtests': () => RUN('queued'),
      'GET /signals-lab/backtests/r1': () => RUN('completed'),
      'GET /signals-lab/data-quality/*': () => ({ status: 'warn', bars: 1875, missing_pct: 1.2, issues: ['22 missing 1-minute candle(s)'] }),
    });
    await mount();
    tab('Test one strategy');
    expect(await screen.findByLabelText('Backtest configuration')).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText('DASH ✕')).toBeInTheDocument());     // universe from the strategy version
    expect(within(screen.getByLabelText('Costs')).getByRole('combobox', { name: 'Cost profile' })).toHaveTextContent('India equity intraday');
    fireEvent.click(screen.getByRole('button', { name: 'Check data quality' }));
    expect(await screen.findByText(/22 missing 1-minute candle/)).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole('button', { name: 'Run backtest' })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: 'Run backtest' }));
    await waitFor(() => expect(calls.some((c) => c.path === '/signals-lab/backtests' && c.method === 'POST')).toBe(true));
    const post = calls.find((c) => c.path === '/signals-lab/backtests' && c.method === 'POST')!.body as { strategy_id: string; version: number; instruments: string[]; config: { slippage_bps: string; max_missing_pct: number } };
    expect(post).toMatchObject({ strategy_id: 's1', version: 2, instruments: ['i1'], config: { slippage_bps: '2', max_missing_pct: 5 } });
    const stats = await screen.findByLabelText('Key results');
    expect(within(stats).getByText('1,234.50')).toBeInTheDocument();
    expect(within(stats).getByText(/Gross P&L \(before slippage & charges\)/)).toBeInTheDocument();
    expect(within(stats).getByText(/Slippage \(model\)/)).toBeInTheDocument();
    expect(within(stats).getByText(/Charges \(brokerage \+ statutory\)/)).toBeInTheDocument();
    const agg = within(screen.getByLabelText('Charges breakdown'));
    expect(agg.getByText('Total turnover')).toBeInTheDocument();
    expect(agg.getByText('0.0412%')).toBeInTheDocument();
    expect(screen.getByText(/only 2 trade\(s\)/)).toBeInTheDocument();
    expect(screen.getByText(/needs >= 30 trading days/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'trades' }));
    const ledger = await screen.findByLabelText('Trade ledger');
    expect(within(ledger).getAllByRole('row').length).toBe(3);
    expect(within(ledger).getByText('ambiguous')).toBeInTheDocument();
    fireEvent.click(within(ledger).getAllByRole('row')[1]);
    expect(screen.getByText(/EMA\(9\)@5m crosses above EMA\(21\)@5m/)).toBeInTheDocument();
    const cost = within(screen.getByLabelText('Cost breakdown'));
    for (const label of ['Brokerage', 'STT', 'Exchange charges', 'SEBI charges', 'IPFT', 'Stamp duty', 'GST', 'Total charges', 'Net P&L']) expect(cost.getByText(label)).toBeInTheDocument();
    expect(cost.getByText(/Gross P&L \(before slippage/)).toBeInTheDocument();
    expect(cost.getByText('Slippage (model, separate)')).toBeInTheDocument();
    expect(cost.getByText('0.0412%')).toBeInTheDocument();
    expect(cost.getByText(/1,00,000.00 \/ 1,01,000.00 \/ 2,01,000.00/)).toBeInTheDocument();
    expect(cost.getByText(/entry 20.00 · exit 20.00/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Sort by Net' }));
    fireEvent.click(screen.getByRole('button', { name: 'equity' }));
    expect(screen.getByRole('img', { name: 'Equity (mark-to-market)' })).toBeInTheDocument();
    expect(screen.getByRole('img', { name: 'Drawdown %' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'monthly' }));
    expect(screen.getByRole('img', { name: 'Net P&L by month' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'assumptions' }));
    expect(screen.getByText(/Costs and fills are modelling assumptions, not broker-confirmed values/)).toBeInTheDocument();
    expect(screen.getByText(/stop assumed first/)).toBeInTheDocument();
    expect(screen.getByText(/Dataset SHA-256/)).toBeInTheDocument();
  }, 15_000);

  it('shows progress while running and the engine error when a run fails', async () => {
    install({ 'POST /signals-lab/backtests': () => RUN('queued'), 'GET /signals-lab/backtests/r1': () => ({ ...RUN('failed'), error: 'no instrument has sufficient clean 1-minute data: DASH: 40 of 375 minutes' }) });
    await mount();
    tab('Test one strategy');
    await waitFor(() => expect(screen.getByText('DASH ✕')).toBeInTheDocument());
    await waitFor(() => expect(screen.getByRole('button', { name: 'Run backtest' })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: 'Run backtest' }));
    expect(await screen.findByText(/Backtest failed: no instrument has sufficient clean 1-minute data/)).toBeInTheDocument();
  });

  it('walk-forward analysis shows only train-chosen parameters and out-of-sample totals', async () => {
    install({
      'POST /signals-lab/backtests': () => ({ ...RUN('queued'), kind: 'walk_forward' }),
      'GET /signals-lab/backtests/r1': () => ({ ...RUN('completed'), kind: 'walk_forward', metrics: null, result: { mode: 'walk_forward', note: 'Each test window uses parameters chosen only from the preceding train window.', optimised_on: 'net_profit (train windows only)', oos_total_trades: 7, oos_net_profit: 321.5,
        folds: [{ train: ['2025-01-06T00:00:00', '2025-01-26T00:00:00'], test: ['2025-01-26T00:00:00', '2025-01-31T00:00:00'], chosen_params: { 'f.length': 11 }, train_result: { net_profit: '900.00' }, test_result: { total_trades: 7, net_profit: '321.50' } }], assumptions: {} } }),
    });
    await mount();
    tab('Test one strategy');
    await waitFor(() => expect(screen.getByText('DASH ✕')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('combobox', { name: 'Analysis type' }));
    fireEvent.mouseDown(screen.getByRole('option', { name: 'Walk-forward' }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Run backtest' })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: 'Run backtest' }));
    const folds = await screen.findByLabelText('Walk-forward folds');
    expect(within(folds).getByText('{"f.length":11}')).toBeInTheDocument();
    expect(screen.getByText(/train windows only/)).toBeInTheDocument();
    expect(screen.getByText(/Each test window uses parameters chosen only from the preceding train window/)).toBeInTheDocument();
  });

  it('live signals render the backend freshness verdict; missing age is unknown, never fresh', async () => {
    const unknown = { ...SIGNAL, id: 'g2', reference_price: '77.00', data_freshness: { stale_after_minutes: 10, bars_used: 800, quality_status: 'ok' } }; // no state / age / as-of
    const stale = { ...SIGNAL, id: 'g3', reference_price: '88.00', data_freshness: { ...SIGNAL.data_freshness, age_minutes: 4300, state: 'stale' } };
    install({ 'GET /signals-lab/signals?*': () => [unknown, stale] });
    await mount();
    tab('Live Signals');
    const u = (await screen.findByText('77.00 INR')).closest('tr') as HTMLElement;
    expect(within(u).getByText('age unknown')).toBeInTheDocument();
    expect(within(u).queryByText('fresh')).toBeNull();
    const st = screen.getByText('88.00 INR').closest('tr') as HTMLElement;
    expect(within(st).getByText('stale')).toBeInTheDocument();
    expect(within(st).getByText(/4300 min/)).toBeInTheDocument(); // true age of an old scan
  });

  it('live signals list is explainable and a scan posts to the API', async () => {
    install({ 'POST /signals-lab/signals/scan': () => ({ scan_run_id: 'run-1', job_ids: ['j'], strategies_queued: 1, skipped_in_flight: [] }), 'GET /signals-lab/scan-runs/run-1': () => ({ scan_run_id: 'run-1', status: 'completed', strategies: 1, pairs_scanned: 1, instruments_scanned: 1, new_signals: 1, errors: [], rejections: { scan_run_id: 'run-1', total: 2, by_reason: { insufficient_bars: 2 } } }) });
    await mount();
    tab('Live Signals');
    const row = (await screen.findByText('101.50 INR')).closest('tr') as HTMLElement;
    expect(within(row).getByText('new')).toBeInTheDocument();
    expect(within(row).getByText('fresh')).toBeInTheDocument();
    fireEvent.click(row);
    expect(await screen.findByText('Conditions satisfied')).toBeInTheDocument();
    expect(screen.getByText(/RSI\(14\)@15m > 55/)).toBeInTheDocument();
    expect(screen.getByText('Not satisfied')).toBeInTheDocument();
    expect(screen.getByText(/Decision support, not investment advice/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Scan now' }));
    await waitFor(() => expect(calls.some((c) => c.path === '/signals-lab/signals/scan' && c.method === 'POST')).toBe(true));
    await waitFor(() => expect(document.querySelector('.sl-scan-summary')?.textContent).toMatch(/1 instrument \(1 strategy\/instrument pair\) scanned · 1 new signal · 2 suppressed/));
    expect(screen.getAllByText(/2 suppressed \(Not enough bars 2\)/).length).toBeGreaterThan(0);
    fireEvent.change(screen.getByLabelText('Signal status'), { target: { value: 'suppressed' } });
    await waitFor(() => expect(calls.some((c) => c.path.includes('status=suppressed'))).toBe(true));
  });
});

describe('Layout safety', () => {
  it('every data table is inside a horizontally scrollable region and layouts collapse on narrow screens', async () => {
    install();
    await mount();
    for (const name of ['Strategy Library', 'Indicator Library', 'Live Signals']) {
      tab(name);
      await waitFor(() => expect(document.querySelectorAll('table').length).toBeGreaterThan(0));
      document.querySelectorAll('table').forEach((t) => expect(t.closest('.sl-scroll')).not.toBeNull());
    }
    expect(css).toMatch(/\.sl-scroll\s*{[^}]*overflow-x:\s*auto/);
    expect(css).toMatch(/\.sl-root\s*{[^}]*min-width:\s*0/);
    expect(css).toMatch(/@media \(max-width: 1000px\)[\s\S]*\.sl-bt-layout\s*{\s*grid-template-columns:\s*minmax\(0, 1fr\)/);
    expect(css).toMatch(/@media \(max-width: 560px\)/);
    expect(css).toMatch(/\.sl-tabs[^{]*{[^}]*overflow-x:\s*auto/);
    cleanup();
  });
});

describe('scalp_hold engine parameters', () => {
  beforeEach(() => { calls = []; });
  afterEach(() => { cleanup(); vi.clearAllMocks(); });

  it('shows an engine-parameter editor for scalp_hold and posts non-blank overrides as options.params', async () => {
    const engineDef = { ...DEF, name: 'scalp_hold', python: { source_sha256: 'x', entrypoint: 'Strategy' }, long_entry: null, source: { type: 'python', provenance: { engine: 'scalp_hold', params: {} } } };
    const engineRow = { ...INTRADAY, id: 'sh', name: 'scalp_hold', source_type: 'python', current_version: 1 };
    install({
      'GET /signals-lab/strategies': () => [engineRow],
      'GET /signals-lab/strategies/sh': () => ({ ...engineRow, versions: [{ version: 1, created_at: '2026-11-12T00:00:00Z', source_type: 'python', definition: engineDef, params: {}, definition_hash: 'h', locked: false, has_source: true, source_filename: 'scalp_hold.py' }] }),
      'POST /signals-lab/backtests': () => RUN('queued'),
      'GET /signals-lab/backtests/r1': () => ({ ...RUN('completed'), result: { mode: 'scalp_hold', kind: 'single', slip_bps: 2, unverified_assumptions: [], refused_instruments: [], instruments: [], trades_total: 0, trades: [], summary: { n: 0 }, note: 'hypothetical', total_combos_evaluated: 0 } }),
    });
    await mount();
    tab('Test one strategy');
    expect(await screen.findByLabelText('Engine parameters')).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('engine signal_types'), { target: { value: 'B' } });
    fireEvent.change(screen.getByLabelText('engine H_minutes'), { target: { value: '60' } });
    fireEvent.change(screen.getByLabelText('engine no_entry_hours_ist'), { target: { value: '11,12,13' } });
    await waitFor(() => expect(screen.getByText('DASH ✕')).toBeInTheDocument());          // universe pre-selected from the strategy version
    await waitFor(() => expect(screen.getByRole('button', { name: 'Run backtest' })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: 'Run backtest' }));
    await waitFor(() => expect(calls.some((c) => c.path === '/signals-lab/backtests' && c.method === 'POST')).toBe(true));
    const post = calls.find((c) => c.path === '/signals-lab/backtests' && c.method === 'POST')!.body as { options: { params: Record<string, string> } };
    expect(post.options.params).toEqual({ signal_types: 'B', H_minutes: '60', no_entry_hours_ist: '11,12,13' });
    expect(await screen.findByLabelText('scalp_hold results')).toBeInTheDocument();
  });
});
