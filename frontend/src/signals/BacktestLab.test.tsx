import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { BacktestLab, type LabRow } from './BacktestLab';
import type { Meta, StrategyRow } from './types';

const calls: { path: string; method: string; body?: Record<string, unknown> }[] = [];
let batchState: { finished: boolean; kind: string; rows: LabRow[] };
const downloads: string[] = [];

vi.mock('../api', () => ({ downloadCsv: async (path: string) => { downloads.push(path); } }));
vi.mock('./lab', async (importOriginal) => {
  const original = await importOriginal<typeof import('./lab')>();
  return {
    ...original,
    lab: async (path: string, init: { method?: string; body?: Record<string, unknown> } = {}) => {
      calls.push({ path, method: init.method ?? 'GET', body: init.body });
      if (path === '/lab/universes') return universes;
      if (path === '/lab/batches' && init.method === 'POST') return { batch_id: 'b1', runs: [{}, {}], rejected: [], excluded: [{ symbol: 'MARUTI', reason: 'not in the instrument master' }], instruments: [{ symbol: 'TCS' }, { symbol: 'INFY' }] };
      if (path === '/lab/batches') return { batches: [{ batch_id: 'b1', name: 'Morning run', kind: 'single', created_at: '2026-11-21T00:00:00Z', runs: 2, completed: 2, failed: 0 }] };
      if (path.startsWith('/lab/compare')) return comparisonBody;
      if (path.startsWith('/lab/batches/b1')) return { batch_id: 'b1', name: 'Morning run', created_at: '2026-11-21T00:00:00Z', window: { start: '2025-11-21T00:00:00Z', end: '2026-11-21T00:00:00Z' }, label: 'Hypothetical backtest results after charges and slippage.', min_trades: 30, progress: { done: batchState.finished ? 2 : 1, total: 2 }, ...batchState };
      throw new Error(`unexpected ${path}`);
    },
  };
});

const comparisonBody = {
  columns: [
    { run_id: 'r1', strategy: 'Alpha', metrics: { total_trades: 120, win_rate: 0.45, gross_pnl_before_costs: '5000.00', total_charges: '700.00', total_slippage: '300.00', net_profit: '4000.00', profit_factor_net: 1.5, expectancy_per_trade: '12.50', max_drawdown_pct: -3.2, sharpe_ratio: 1.1 }, monthly: { '2026-01': '1500.00', '2026-02': '2500.00' }, by_instrument: { TCS: '3000.00', INFY: '1000.00' } },
    { run_id: 'r2', strategy: 'Beta', metrics: { total_trades: 200, win_rate: 0.4, gross_pnl_before_costs: '-100.00', total_charges: '900.00', total_slippage: '400.00', net_profit: '-2500.00', profit_factor_net: 0.8, expectancy_per_trade: '-12.50', max_drawdown_pct: -6.0, sharpe_ratio: -0.7 }, monthly: { '2026-01': '-1000.00', '2026-02': '-1500.00' }, by_instrument: { TCS: '-500.00', INFY: '-2000.00' } },
  ],
  shared_symbols: ['INFY', 'TCS'], months: ['2026-01', '2026-02'], metric_keys: ['total_trades', 'win_rate', 'gross_pnl_before_costs', 'total_charges', 'total_slippage', 'net_profit', 'profit_factor_net', 'expectancy_per_trade', 'max_drawdown_pct', 'sharpe_ratio'],
  best: { net_profit: 'r1', profit_factor_net: 'r1', expectancy_per_trade: 'r1', sharpe_ratio: 'r1' }, note: 'Hypothetical backtests. Compare runs only when their window, symbols and charge schedule match; differences otherwise are not like-for-like.',
};

const universes = { max_strategies: 12, max_instruments: 50, note: 'Intraday timeframes are built from stored 1-minute bars.', presets: [
  { key: 'watchlist', label: 'My watchlist', available: 1, symbols: ['SBIN'], excluded: [], min_bars_1m: 20000 },
  { key: 'nifty50', label: 'Nifty 50', available: 2, symbols: ['TCS', 'INFY'], min_bars_1m: 20000, excluded: [{ symbol: 'MARUTI', reason: 'not in the instrument master' }, { symbol: 'WIPRO', reason: 'only 375 stored 1-minute bars (need 20000)' }] },
] };

const meta = { cost_profiles: [{ id: 'dhan_nse_intraday', label: 'Dhan · NSE · Intraday', market: 'equity_in' }, { id: 'crypto_x', label: 'Crypto', market: 'crypto' }] } as unknown as Meta;
const strat = (id: string, name: string): StrategyRow => ({ id, name, kind: 'intraday', status: 'suspended', decision_timeframe: '5m', origin_label: null } as unknown as StrategyRow);
const strategies = [strat('s1', 'Alpha'), strat('s2', 'Beta'), { ...strat('s3', 'Legacy'), kind: 'legacy_daily' } as StrategyRow, { ...strat('s4', 'Old'), status: 'retired' } as StrategyRow];

const metrics = (net: string, pf: number, trades: number) => ({ total_trades: trades, win_rate: 0.45, net_profit: net, profit_factor_net: pf, expectancy_per_trade: '12.50', max_drawdown_pct: -3.2, gross_pnl_before_costs: '5000.00', total_charges: '700.00', total_slippage: '300.00' });
const row = (over: Partial<LabRow>): LabRow => ({ run_id: 'r', strategy_id: 's', strategy: 'X', kind: 'single', status: 'completed', rank: 1, low_sample: false, metrics: metrics('4000.00', 1.5, 120),
  by_instrument: { TCS: { trades: 60, net_pnl: '3000.00', win_rate: 0.5, expectancy: '50.00' }, INFY: { trades: 60, net_pnl: '-500.00', win_rate: 0.4, expectancy: '-8.33' } }, symbols_profitable: 1, symbols_tested: 2, months_profitable: 7, months_tested: 12, cost_profile: 'Dhan · NSE · Intraday', refused_instruments: [], ...over });

beforeEach(() => {
  calls.length = 0; downloads.length = 0;
  batchState = { finished: true, kind: 'single', rows: [row({ run_id: 'r1', strategy: 'Alpha', rank: 1 }), row({ run_id: 'r2', strategy: 'Beta', rank: 2, metrics: metrics('-2500.00', 0.8, 200) }),
    row({ run_id: 'r3', strategy: 'Gamma', rank: null, low_sample: true, metrics: metrics('900.00', 9.9, 4) })] };
});
afterEach(() => { cleanup(); vi.useRealTimers(); });

const strategyNames = () => within(screen.getByRole('list', { name: 'Strategies to compare' })).getAllByRole('checkbox').map((c) => c.closest('label')!.querySelector('strong')!.textContent);
const tableOrder = () => Array.from(document.querySelectorAll('.lab-table tbody > tr:not(.lab-detail)')).map((tr) => tr.querySelector('strong')?.textContent);

const inResults = () => within(screen.getByRole('region', { name: 'Batch results' }));

async function runBatch() {
  render(<BacktestLab meta={meta} strategies={strategies} />);
  await screen.findByText(/Nifty 50 — 2 usable/);
  fireEvent.click(screen.getByLabelText(/Alpha/));
  fireEvent.click(screen.getByLabelText(/Beta/));
  fireEvent.click(screen.getByRole('button', { name: /Run 2 strategies on 2 symbols/ }));
  await screen.findByRole('region', { name: 'Batch results' });
}

describe('BacktestLab', () => {
  it('always says results are hypothetical, lists only usable intraday strategies and shows symbol-set coverage', async () => {
    render(<BacktestLab meta={meta} strategies={strategies} />);
    expect(await screen.findByText(/Hypothetical backtests on stored real 1-minute bars/)).toBeTruthy();
    expect(strategyNames()).toEqual(['Alpha', 'Beta']);                     // legacy daily and retired strategies are not offered
    fireEvent.click(await screen.findByText(/2 symbols with enough 1-minute data · 2 left out/));
    expect(screen.getByText(/WIPRO/)).toBeTruthy();
    expect(screen.getByText(/only 375 stored 1-minute bars/)).toBeTruthy();   // exactly why each name was left out
    expect(screen.getByText(/MARUTI/)).toBeTruthy();
    const charges = screen.getByLabelText('Charge schedule');
    expect(within(charges).getAllByRole('option').map((o) => o.textContent)).toEqual(['Default for the market (Dhan · NSE · intraday)', 'Dhan · NSE · Intraday']);   // no crypto schedule
  });

  it('will not run without a strategy and posts the chosen strategies, symbol set, window, slippage and mode', async () => {
    render(<BacktestLab meta={meta} strategies={strategies} />);
    await screen.findByText(/Nifty 50 — 2 usable/);
    expect((screen.getByRole('button', { name: /Run/ }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByLabelText(/Alpha/));
    fireEvent.change(screen.getByLabelText('Charge schedule'), { target: { value: 'dhan_nse_intraday' } });
    fireEvent.change(screen.getByLabelText('Mode'), { target: { value: 'is_oos' } });
    fireEvent.click(screen.getByRole('button', { name: /Run 1 strategy on 2 symbols/ }));
    await screen.findByRole('region', { name: 'Batch results' });
    const post = calls.find((c) => c.method === 'POST')!;
    expect(post.path).toBe('/lab/batches');
    expect(post.body).toMatchObject({ strategy_ids: ['s1'], universe: 'nifty50', kind: 'is_oos', split_ratio: 0.7 });
    expect(post.body!.config).toMatchObject({ slippage_bps: '2', cost_profile: 'dhan_nse_intraday' });
    expect(String((post.body!.config as Record<string, string>).start)).toMatch(/^\d{4}-\d{2}-\d{2}T00:00:00Z$/);
    expect(await screen.findByText(/1 symbol\(s\) left out for lack of stored 1-minute data/)).toBeTruthy();
  });

  it('shows the ranked table with gross, charges, slippage and net, and flags thin rows instead of ranking them', async () => {
    await runBatch();
    expect(tableOrder()).toEqual(['Alpha', 'Beta', 'Gamma']);
    const alpha = inResults().getByText('Alpha').closest('tr')!;
    expect(within(alpha).getByText('120')).toBeTruthy();
    expect(within(alpha).getByText((5000).toLocaleString('en-IN', { minimumFractionDigits: 2 }))).toBeTruthy();    // gross
    expect(within(alpha).getByText('700.00')).toBeTruthy();                                                         // charges are shown, not hidden
    expect(within(alpha).getByText('1.50')).toBeTruthy();
    const gamma = inResults().getByText('Gamma').closest('tr')!;
    expect(within(gamma).getByText('thin')).toBeTruthy();
    expect(within(gamma).getByText('—', { selector: 'td' })).toBeTruthy();                                         // unranked: no rank number
    expect(screen.getByText('finished')).toBeTruthy();
    expect(screen.getByText(/Hypothetical backtest results after charges and slippage/)).toBeTruthy();
  });

  it('sorts by a column both ways and expands a row to its per-symbol results', async () => {
    await runBatch();
    fireEvent.click(screen.getByRole('button', { name: /^Net/ }));      // descending first
    expect(tableOrder()).toEqual(['Alpha', 'Gamma', 'Beta']);
    fireEvent.click(screen.getByRole('button', { name: /^Net/ }));
    expect(tableOrder()).toEqual(['Beta', 'Gamma', 'Alpha']);
    fireEvent.click(inResults().getByText('Alpha'));
    const detail = within(await screen.findByRole('table', { name: 'Alpha by symbol' }));
    const symbols = detail.getAllByRole('row').slice(1).map((r) => r.querySelector('td')!.textContent);
    expect(symbols).toEqual(['TCS', 'INFY']);                           // best symbol first
    expect(screen.getByText(/Profitable months: 7\/12/)).toBeTruthy();
  });

  it('polls a running batch until every run has finished, then stops', async () => {
    batchState = { finished: false, kind: 'single', rows: [row({ run_id: 'r1', strategy: 'Alpha' }), row({ run_id: 'r2', strategy: 'Beta', status: 'running', rank: null, metrics: undefined, by_instrument: null })] };
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
    render(<BacktestLab meta={meta} strategies={strategies} />);
    await screen.findByText(/Nifty 50 — 2 usable/);
    fireEvent.click(screen.getByLabelText(/Alpha/));
    fireEvent.click(screen.getByRole('button', { name: /Run 1 strategy/ }));
    await screen.findByText(/running 1\/2/);
    expect(screen.getByText('running')).toBeTruthy();
    batchState = { ...batchState, finished: true, rows: batchState.rows.map((r) => (r.run_id === 'r2' ? row({ run_id: 'r2', strategy: 'Beta', rank: 2 }) : r)) };
    await act(async () => { await vi.advanceTimersByTimeAsync(2600); });
    await screen.findByText('finished');
    const polls = () => calls.filter((c) => c.path.startsWith('/lab/batches/b1')).length;
    const settled = polls();
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
    expect(polls()).toBe(settled);                                       // no polling after completion
  });

  it('shows in-sample and out-of-sample for that mode and exports the batch as CSV', async () => {
    batchState = { finished: true, kind: 'is_oos', rows: [row({ run_id: 'r1', strategy: 'Alpha', by_instrument: null, in_sample: { profit_factor_net: 1.9, net_profit: '9000.00' }, out_of_sample: { profit_factor_net: 0.7, net_profit: '-1200.00' }, split_at: '2026-06-01T00:00:00Z', metrics: metrics('-1200.00', 0.7, 80) })] };
    await runBatch();
    expect(screen.getByText('OOS net')).toBeTruthy();
    expect(screen.getByText('IS PF')).toBeTruthy();
    expect(screen.getByText('1.90')).toBeTruthy();                       // in-sample looks great ...
    expect(screen.getByText('0.70')).toBeTruthy();                       // ... out-of-sample does not: that is the point of showing both
    fireEvent.click(inResults().getByText('Alpha'));
    expect(await screen.findByText(/in-sample net .*9,000\.00/i)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Export CSV' }));
    expect(downloads).toEqual(['/signals-lab/lab/batches/b1/export.csv']);
  });

  it('reopens an earlier batch from history', async () => {
    render(<BacktestLab meta={meta} strategies={strategies} />);
    fireEvent.click(await screen.findByRole('button', { name: /Morning run/ }));
    expect(await screen.findByRole('region', { name: 'Batch results' })).toBeTruthy();
    await waitFor(() => expect(tableOrder()).toEqual(['Alpha', 'Beta', 'Gamma']));
  });
});


describe('BacktestLab · walk-forward, sweeps and comparison', () => {
  it('offers all four modes and sends the walk-forward windows or the sweep steps', async () => {
    render(<BacktestLab meta={meta} strategies={strategies} />);
    await screen.findByText(/Nifty 50 — 2 usable/);
    expect(within(screen.getByLabelText('Mode')).getAllByRole('option').map((o) => o.textContent)).toEqual([
      'Single run (whole window)', 'In-sample vs out-of-sample', 'Walk-forward (rolling train → test)', 'Parameter sweep (robustness)']);
    fireEvent.click(screen.getByLabelText(/Alpha/));
    fireEvent.change(screen.getByLabelText('Mode'), { target: { value: 'walk_forward' } });
    fireEvent.change(screen.getByLabelText('Train days'), { target: { value: '30' } });
    fireEvent.change(screen.getByLabelText('Test days'), { target: { value: '7' } });
    fireEvent.click(screen.getByRole('button', { name: /Run 1 strategy/ }));
    await screen.findByRole('region', { name: 'Batch results' });
    expect(calls.find((c) => c.method === 'POST')!.body).toMatchObject({ kind: 'walk_forward', train_days: 30, test_days: 7 });
    cleanup(); calls.length = 0;
    render(<BacktestLab meta={meta} strategies={strategies} />);
    await screen.findByText(/Nifty 50 — 2 usable/);
    fireEvent.click(screen.getByLabelText(/Alpha/));
    fireEvent.change(screen.getByLabelText('Mode'), { target: { value: 'sensitivity' } });
    fireEvent.change(screen.getByLabelText('Grid steps'), { target: { value: '2' } });
    fireEvent.click(screen.getByRole('button', { name: /Run 1 strategy/ }));
    await screen.findByRole('region', { name: 'Batch results' });
    expect(calls.find((c) => c.method === 'POST')!.body).toMatchObject({ kind: 'sensitivity', steps: 2 });
  });

  it('shows walk-forward folds: each test window with the parameters chosen on the train window', async () => {
    batchState = { finished: true, kind: 'walk_forward', rows: [row({ run_id: 'w1', strategy: 'Alpha', metrics: { total_trades: 90, net_profit: '1200.00' }, by_instrument: null, symbols_tested: undefined,
      walk_forward: { folds: 3, folds_profitable: 2, train_days: 20, test_days: 5, parameters: ['stop.value'], optimised_on: 'net_profit (train windows only)', fold_results: [
        { test: ['2026-01-05T00:00:00Z', '2026-01-10T00:00:00Z'], chosen_params: { 'stop.value': 1.5 }, net_profit: '800.00', trades: 30 },
        { test: ['2026-01-10T00:00:00Z', '2026-01-15T00:00:00Z'], chosen_params: {}, net_profit: '-300.00', trades: 28 }] } })] };
    await runBatch();
    expect(screen.getByText('Test-window net')).toBeTruthy();
    expect(screen.queryByText('PF')).toBeNull();                          // no trade-level profit factor for rolling folds
    const r = inResults().getByText('Alpha').closest('tr')!;
    expect(within(r).getByText('2/3')).toBeTruthy();                      // profitable folds
    fireEvent.click(r);
    const folds = within(await screen.findByRole('table', { name: 'Alpha folds' }));
    expect(folds.getByText('2026-01-05 → 2026-01-10')).toBeTruthy();
    expect(folds.getByText('stop.value=1.5')).toBeTruthy();
    expect(folds.getByText('defaults')).toBeTruthy();
    expect(screen.getByText(/train windows only/)).toBeTruthy();
  });

  it('shows a sweep as median / worst / best with the grid, so one lucky setting is visible as such', async () => {
    batchState = { finished: true, kind: 'sensitivity', rows: [row({ run_id: 'x1', strategy: 'Alpha', metrics: { total_trades: 100, net_profit: '-400.00' }, by_instrument: null,
      sweep: { parameters: ['stop.value'], points: 3, points_profitable: 1, best: '2500.00', worst: '-1800.00', median: '-400.00', window: 'in-sample only', note: 'Computed on the in-sample window.',
        grid: [{ params: { 'stop.value': 1.1 }, total_trades: 100, net_profit: '-1800.00', profit_factor_net: 0.7 }, { params: { 'stop.value': 1.5 }, total_trades: 100, net_profit: '-400.00', profit_factor_net: 0.9 }, { params: { 'stop.value': 1.9 }, total_trades: 100, net_profit: '2500.00', profit_factor_net: 1.4 }] } })] };
    await runBatch();
    const r = inResults().getByText('Alpha').closest('tr')!;
    expect(within(r).getByText('1/3')).toBeTruthy();
    fireEvent.click(r);
    const grid = within(await screen.findByRole('table', { name: 'Alpha sweep' }));
    expect(grid.getAllByRole('row')).toHaveLength(4);
    expect(grid.getByText('stop.value=1.9')).toBeTruthy();
    expect(screen.getByText(/result collapses when a parameter moves is overfit/)).toBeTruthy();
  });

  it('compares 2-6 single runs side by side with the best value per metric marked', async () => {
    await runBatch();
    expect(screen.queryByRole('button', { name: /Compare/ })).toBeNull();
    fireEvent.click(screen.getByRole('checkbox', { name: 'Compare Alpha' }));
    expect((screen.getByRole('button', { name: /Compare 1 runs/ }) as HTMLButtonElement).disabled).toBe(true);   // needs two
    fireEvent.click(screen.getByRole('checkbox', { name: 'Compare Beta' }));
    fireEvent.click(screen.getByRole('button', { name: /Compare 2 runs/ }));
    const panel = within(await screen.findByRole('region', { name: 'Run comparison' }));
    expect(calls.some((c) => c.path === '/lab/compare?runs=r1,r2')).toBe(true);
    expect(panel.getByText(/not like-for-like/)).toBeTruthy();
    const netRow = panel.getByText('Net profit').closest('tr')!;
    expect(within(netRow).getByText((4000).toLocaleString('en-IN', { minimumFractionDigits: 2 })).className).toContain('lab-best');
    expect(within(netRow).getByText((-2500).toLocaleString('en-IN', { minimumFractionDigits: 2 })).className).not.toContain('lab-best');
    expect(panel.getByText('2026-02')).toBeTruthy();                      // monthly net
    expect(panel.getByText('INFY')).toBeTruthy();                         // shared symbols only
  });
});
