import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { CryptoDeltaPage } from './CryptoDelta';

vi.mock('./DeltaResearch', () => ({ DeltaResearchPage: () => <div>delta research stub</div> }));
vi.mock('../crypto/CryptoCharts', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../crypto/CryptoCharts')>();
  return {
    ...actual, // keep the real exitMarker / MarkerLegend; only the canvas charts are stubbed
    CryptoCandleChart: ({ candles, markers = [] }: { candles: unknown[]; markers?: unknown[] }) => <div data-testid="candle-chart">{candles.length} candles, {markers.length} markers</div>,
    EquityChart: ({ points }: { points: unknown[] }) => <div data-testid="equity-chart">{points.length} points</div>,
  };
});

const status = {
  mode: { configured: 'research', effective: 'research', live_blocked: false, reason: null },
  capabilities: { order_transmission: false, live_adapter: true, execution_credentials: 'read from backend secrets only', simulated_fills: 'backtest only' },
  limits: { currency: 'USD', max_order_notional: '1000', max_total_exposure: '5000', max_price_age_seconds: 120 },
  audit_events: 0,
  disclaimer: 'Decision support, not investment advice.',
};
const dryRun = {
  mode: 'research', client_order_id: 'tc0123456789abcdef0123456789ab',
  decision: {
    approved: false, notional: '120.000', currency: 'USD', reasons: ['exposure_verifiable: unverifiable'],
    checks: [{ name: 'price_fresh', passed: true, detail: 'price is 3s old' }, { name: 'exposure_verifiable', passed: false, detail: 'current exposure is unverifiable' }],
  },
  order_transmission: 'not_available', note: 'An approved decision means the risk checks would pass. It is not an order and none is sent.',
};
const markets = { items: [
  { product_id: '1', symbol: 'BTCUSD', state: 'live', quote_time: null, last_price: '61000.5', mark_price: '61001', volume_24h: '5000000' },
  { product_id: '2', symbol: 'SOLUSD', state: 'live', quote_time: null, last_price: '150.2', mark_price: '150.3', volume_24h: '90000000' },
] };
const candles = { symbol: 'BTCUSD', resolution: '15m', source: 'delta_rest_history', count: 3, gaps: 0,
  candles: [1, 2, 3].map((n) => ({ timestamp: `2026-11-20T10:0${n}:00Z`, open: '1', high: '2', low: '1', close: '2', volume: '5' })) };
const strategies = { resolutions: ['1m', '5m', '15m'], max_days: { '15m': 120 }, strategies: [
  { id: 'ema_cross', name: 'EMA crossover (trend)', description: 'Long while fast above slow.', params: [
    { key: 'fast_period', label: 'Fast EMA', kind: 'int', default: '12', minimum: '2', maximum: '200' },
    { key: 'stop_atr_mult', label: 'Stop distance (× ATR, 0 = none)', kind: 'decimal', default: '2', minimum: '0', maximum: '20' },
    { key: 'allow_short', label: 'Allow short positions', kind: 'bool', default: 'true', minimum: null, maximum: null },
  ] },
] };
const run = {
  id: 'r1', created_at: '2026-11-20T10:00:00Z', strategy_id: 'ema_cross', strategy_name: 'EMA crossover (trend)', symbol: 'BTCUSD', resolution: '15m',
  window_start: '2026-10-20T00:00:00Z', window_end: '2026-11-20T00:00:00Z', bars_used: 2880, status: 'completed', hypothetical: true,
  label: 'Hypothetical backtest on historical candles. Not a position, an order, or real P&L.',
  params: { fast_period: '12', stop_atr_mult: '2', allow_short: 'True' },
  metrics: { liquidations: 1, skipped_entries_margin: 3, max_notional_usd: '450.00', closed_trades: 2, net_pnl: '-12.50', return_pct: '-1.25', win_rate_pct: '50.00', profit_factor: '0.60', expectancy: '-6.25', max_drawdown: '20.00', max_drawdown_pct: '2.00', total_fees: '3.10', exposure_pct: '40.00' },
  assumptions: { initial_capital_usd: '1000', sizing_mode: 'contracts', sizing_value: '10', units_per_contract: '0.001', margin: { model: 'isolated margin at the chosen leverage', leverage: '10', max_leverage: '200', initial_margin_pct: '0.5', maintenance_margin_pct: '0.25', liquidation_penalty_factor: '0.5', position_size_limit: 125000 }, fee_rate_per_fill: '0.0005', fee_source: 'exchange product data', contract_value: '0.001', tick_size: '0.5', contracts_per_trade: '10', slippage_bps: '2', fill_model: 'signal at bar close, fill at next bar open', not_modelled: ['funding payments', 'liquidation'] },
  data_quality: { bars: 2880, expected_bars: 2880, coverage_pct: '100.0', gaps: 0, missing_bars: 0, source: 'Delta Exchange India public history (completed candles)', downloaded: false },
  trades: [
    { side: 'long', contracts: '10', entry_time: '2026-11-20T10:01:00Z', exit_time: '2026-11-20T10:02:00Z', entry_price: '1.0', exit_price: '2.0', gross_pnl: '1.00', fees: '0.10', net_pnl: '0.90', exit_reason: 'signal', bars_held: 1 },
    { side: 'short', contracts: '10', entry_time: '2026-11-20T10:02:00Z', exit_time: '2026-11-20T10:03:00Z', entry_price: '2.0', exit_price: '3.0', gross_pnl: '-1.00', fees: '0.10', net_pnl: '-1.10', exit_reason: 'stop', bars_held: 1 },
  ],
  equity: [{ time: '2026-11-20T10:01:00Z', equity: '1000.00' }, { time: '2026-11-20T10:02:00Z', equity: '999.00' }],
};

function mockApi() {
  const calls: { url: string; init?: RequestInit }[] = [];
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    calls.push({ url, init });
    const path = String(url);
    const body = path.includes('/execution/status') ? status
      : path.includes('/execution/audit') ? []
        : path.includes('/execution/dry-run') ? dryRun
          : path.includes('/delta-research/markets') ? markets
            : path.includes('/crypto-delta/candles/sync') ? { fetched: 100, inserted: 90, incomplete_skipped: 1 }
              : path.includes('/crypto-delta/candles') ? candles
                : path.includes('/crypto-delta/contracts/') ? { symbol: path.includes('/contracts/DOGEUSD') ? 'DOGEUSD' : 'BTCUSD', contract_value: path.includes('/contracts/DOGEUSD') ? '100' : '0.001', units_per_contract: path.includes('/contracts/DOGEUSD') ? '100' : '0.001', tick_size: '0.5', taker_fee_rate: '0.0005', maker_fee_rate: '0.0002', quote_currency: 'USD', source: 'Delta Exchange product data', initial_margin_pct: path.includes('/contracts/DOGEUSD') ? '1' : '0.5', maintenance_margin_pct: path.includes('/contracts/DOGEUSD') ? '0.5' : '0.25', max_leverage: path.includes('/contracts/DOGEUSD') ? '100' : '200', position_size_limit: path.includes('/contracts/DOGEUSD') ? 50000 : 125000 }
                  : path.includes('/crypto-delta/strategies') ? strategies
                    : path.includes('/crypto-delta/backtests/r1') ? (init?.method === 'DELETE' ? {} : run)
                      : path.includes('/crypto-delta/backtests') ? (init?.method === 'POST' ? run : [run])
                        : {};
    return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } });
  }));
  return calls;
}

beforeEach(() => window.localStorage.clear());
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe('CryptoDeltaPage', () => {
  it('opens on Markets & charts with a watch list sorted by volume and a chart of completed candles', async () => {
    mockApi();
    render(<CryptoDeltaPage />);
    const watch = await screen.findByRole('region', { name: 'Perpetual contracts' });
    await within(watch).findByText('SOLUSD');
    const bodyRows = within(watch).getAllByRole('row').slice(1);
    expect(bodyRows[0].textContent).toContain('SOLUSD'); // 90M volume ranks above BTC's 5M
    expect(await screen.findByTestId('candle-chart')).toHaveProperty('textContent', '3 candles, 0 markers');
    expect(await screen.findByText('0.050%')).toBeTruthy(); // exchange taker fee, shown not assumed
    fireEvent.click(within(watch).getByRole('button', { name: 'SOLUSD' }));
    await waitFor(() => expect(window.localStorage.getItem('crypto.symbol')).toBe('SOLUSD'));
  });

  it('downloads candles from the public history for the chosen size', async () => {
    const calls = mockApi();
    render(<CryptoDeltaPage />);
    fireEvent.click(await screen.findByRole('button', { name: 'Download 30d of 15m candles' }));
    expect(await screen.findByText(/Downloaded 100 candles from Delta; 90 new, 1 still forming/)).toBeTruthy();
    const post = calls.find((call) => call.url.includes('/candles/sync'));
    expect(JSON.parse(String(post?.init?.body))).toEqual({ symbol: 'BTCUSD', resolution: '15m', days: 30 });
  });

  it('runs a backtest from the strategy lab and labels everything hypothetical', async () => {
    const calls = mockApi();
    render(<CryptoDeltaPage />);
    fireEvent.click(await screen.findByRole('tab', { name: 'Strategy lab' }));
    await screen.findByText(/Long while fast above slow/);
    fireEvent.click(await screen.findByRole('button', { name: 'Run backtest' }));
    expect(await screen.findByText(/Not a position, an order, or real P&L/)).toBeTruthy();
    expect(screen.getByText('-12.50 USD')).toBeTruthy();
    expect(await screen.findByText('3 candles, 4 markers')).toBeTruthy(); // 2 trades -> entry + exit markers
    expect(screen.getByTestId('equity-chart').textContent).toBe('2 points');
    expect(screen.getByLabelText('Chart marker legend').textContent).toContain('T target hit');
    expect(screen.getByText(/Not modelled: funding payments, liquidation/)).toBeTruthy();
    const post = calls.find((call) => call.url.endsWith('/crypto-delta/backtests') && call.init?.method === 'POST');
    const sent = JSON.parse(String(post?.init?.body));
    // numbers are strings (money) or integers (periods); never floats
    expect(sent).toMatchObject({ strategy_id: 'ema_cross', symbol: 'BTCUSD', resolution: '15m', contracts: '10', initial_capital: '1000', slippage_bps: '2', leverage: '10', sizing_mode: 'contracts' });
    expect(sent.params).toEqual({ fast_period: 12, stop_atr_mult: '2', allow_short: true });
  });

  it('re-downloads a run\'s candles when they are no longer stored, so the chart is never silently empty', async () => {
    const calls: { url: string; init?: RequestInit }[] = [];
    let stored = false;
    vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({ url: String(url), init });
      const path = String(url);
      const body = path.includes('/crypto-delta/candles/sync') ? (stored = true, { fetched: 2880, inserted: 2880, incomplete_skipped: 0 })
        : path.includes('/crypto-delta/candles') ? (stored ? { ...candles, count: 2880 } : { ...candles, count: 0, candles: [] })
          : path.includes('/crypto-delta/strategies') ? strategies
            : path.includes('/crypto-delta/backtests') ? (init?.method === 'POST' ? run : [run])
              : path.includes('/delta-research/markets') ? markets : {};
      return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } });
    }));
    render(<CryptoDeltaPage />);
    fireEvent.click(await screen.findByRole('tab', { name: 'Strategy lab' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Run backtest' }));
    expect(await screen.findByText('Candles were re-downloaded from Delta for this run.')).toBeTruthy();
    expect((await screen.findByTestId('candle-chart')).textContent).toBe('3 candles, 4 markers'); // the stub renders the mocked array
    const sync = calls.find((call) => call.url.includes('/candles/sync'));
    expect(JSON.parse(String(sync?.init?.body))).toEqual({ symbol: 'BTCUSD', resolution: '15m', start: run.window_start, end: run.window_end });
  });

  it('states plainly on the Execution tab that orders are not placed', async () => {
    mockApi();
    render(<CryptoDeltaPage />);
    fireEvent.click(await screen.findByRole('tab', { name: 'Execution' }));
    expect(await screen.findByText('Delta orders: off until armed')).toBeTruthy();
    expect(await screen.findByText('Switched off')).toBeTruthy();
    expect(screen.getByText('1000 USD')).toBeTruthy();
  });

  it('runs a risk check and shows blocked results with the unverifiable exposure reason', async () => {
    const calls = mockApi();
    render(<CryptoDeltaPage />);
    fireEvent.click(await screen.findByRole('tab', { name: 'Execution' }));
    await screen.findByText('Delta orders: off until armed');
    fireEvent.change(screen.getByLabelText(/Limit price/), { target: { value: '60000' } });
    fireEvent.click(screen.getByRole('button', { name: 'Run risk check' }));
    expect(await screen.findByText(/Blocked by the risk checks/)).toBeTruthy();
    expect(screen.getByText('✕ Current exposure verifiable')).toBeTruthy();
    const post = calls.find((call) => call.url.includes('/execution/dry-run'));
    expect(JSON.parse(String(post?.init?.body)).intent).toMatchObject({ symbol: 'BTCUSD', side: 'buy', order_type: 'limit', limit_price: '60000', quantity: '1' });
  });

  it('shows each contract\'s own lot size, margin and leverage limit, and caps the leverage field to what the contract allows', async () => {
    mockApi();
    render(<CryptoDeltaPage />);
    fireEvent.click(await screen.findByRole('tab', { name: 'Strategy lab' }));
    const info = await screen.findByLabelText('Contract lot and margin');
    expect(info.textContent).toContain('1 contract = 0.001 of the underlying');
    expect(info.textContent).toContain('initial margin 0.5% (max leverage 200x)');
    expect(info.textContent).toContain('maintenance margin 0.25%');
    expect(info.textContent).toContain('125,000 contracts');
    fireEvent.change(screen.getByLabelText('Leverage'), { target: { value: '150' } });
    fireEvent.change(screen.getByLabelText('Contract'), { target: { value: 'DOGEUSD' } });
    await waitFor(() => expect(screen.getByLabelText('Contract lot and margin').textContent).toContain('1 contract = 100 of the underlying'));
    expect(screen.getByLabelText('Contract lot and margin').textContent).toContain('max leverage 100x');
    expect((screen.getByLabelText('Leverage') as HTMLInputElement).value).toBe('100');       // 150x is not allowed on this contract
  });

  it('sizes by USD notional or % risked, sending exactly the matching field', async () => {
    const calls = mockApi();
    render(<CryptoDeltaPage />);
    fireEvent.click(await screen.findByRole('tab', { name: 'Strategy lab' }));
    await screen.findByText(/Long while fast above slow/);
    fireEvent.change(screen.getByLabelText('Position size'), { target: { value: 'notional_usd' } });
    fireEvent.change(screen.getByLabelText('Size value'), { target: { value: '500' } });
    fireEvent.change(screen.getByLabelText('Leverage'), { target: { value: '25' } });
    fireEvent.click(screen.getByRole('button', { name: 'Run backtest' }));
    await screen.findByText(/Not a position, an order, or real P&L/);
    let sent = JSON.parse(String(calls.filter((c) => c.url.endsWith('/crypto-delta/backtests') && c.init?.method === 'POST').at(-1)?.init?.body));
    expect(sent).toMatchObject({ sizing_mode: 'notional_usd', notional_usd: '500', leverage: '25' });
    expect(sent.risk_pct).toBeUndefined();
    fireEvent.change(screen.getByLabelText('Position size'), { target: { value: 'risk_pct' } });
    fireEvent.change(screen.getByLabelText('Size value'), { target: { value: '1' } });
    fireEvent.click(screen.getByRole('button', { name: 'Run backtest' }));
    await waitFor(() => expect(calls.filter((c) => c.url.endsWith('/crypto-delta/backtests') && c.init?.method === 'POST')).toHaveLength(2));
    sent = JSON.parse(String(calls.filter((c) => c.url.endsWith('/crypto-delta/backtests') && c.init?.method === 'POST').at(-1)?.init?.body));
    expect(sent).toMatchObject({ sizing_mode: 'risk_pct', risk_pct: '1' });
    expect(sent.notional_usd).toBeUndefined();
  });

  it('shows the strategy and every parameter the run used, plus capital, leverage, sizing, fees and margin', async () => {
    mockApi();
    render(<CryptoDeltaPage />);
    fireEvent.click(await screen.findByRole('tab', { name: 'Strategy lab' }));
    await screen.findByText(/Long while fast above slow/);
    fireEvent.click(await screen.findByRole('button', { name: 'Run backtest' }));
    const settings = within(await screen.findByLabelText('Run settings'));
    expect(settings.getByText('Strategy').nextSibling?.textContent).toBe('EMA crossover (trend)');
    expect(settings.getByText('Fast EMA').nextSibling?.textContent).toBe('12');                          // labelled from the strategy's own parameter list
    expect(settings.getByText('Stop distance (× ATR, 0 = none)').nextSibling?.textContent).toBe('2');
    expect(settings.getByText('Allow short positions').nextSibling?.textContent).toBe('True');
    expect(settings.getByText('Starting capital').nextSibling?.textContent).toBe('1000 USD');
    expect(settings.getByText('Leverage').nextSibling?.textContent).toBe('10x (max 200x)');
    expect(settings.getByText('Margin').nextSibling?.textContent).toBe('initial 0.5% · maintenance 0.25%');
    expect(settings.getByText('Position size').nextSibling?.textContent).toBe('10 contracts per trade');
    expect(settings.getByText('Lot size').nextSibling?.textContent).toBe('0.001 per contract');
    expect(settings.getByText('Fee / slippage').nextSibling?.textContent).toBe('0.0005 per fill · 2 bps');
    expect(screen.getByText('Liquidations').nextSibling?.textContent).toBe('1');
    expect(screen.getByText('Entries skipped (margin)').nextSibling?.textContent).toBe('3');
  });

  it('deletes a stored run only after confirmation, from the history list or the open result', async () => {
    const calls = mockApi();
    render(<CryptoDeltaPage />);
    fireEvent.click(await screen.findByRole('tab', { name: 'Strategy lab' }));
    const del = await screen.findByRole('button', { name: 'Delete EMA crossover (trend) BTCUSD run' });
    fireEvent.click(del);
    expect(calls.some((c) => c.init?.method === 'DELETE')).toBe(false);                    // nothing happens until it is confirmed
    fireEvent.click(within(screen.getByRole('group', { name: 'Confirm delete' })).getByRole('button', { name: 'Keep' }));
    expect(calls.some((c) => c.init?.method === 'DELETE')).toBe(false);
    fireEvent.click(screen.getByRole('button', { name: 'Delete EMA crossover (trend) BTCUSD run' }));
    fireEvent.click(within(screen.getByRole('group', { name: 'Confirm delete' })).getByRole('button', { name: 'Delete for good' }));
    await waitFor(() => expect(calls.some((c) => c.init?.method === 'DELETE' && c.url.endsWith('/crypto-delta/backtests/r1'))).toBe(true));
    // from the open result
    fireEvent.click(await screen.findByRole('button', { name: 'Open' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Delete this run' }));
    fireEvent.click(within(screen.getByRole('group', { name: 'Confirm delete this run' })).getByRole('button', { name: 'Delete this run for good' }));
    await waitFor(() => expect(calls.filter((c) => c.init?.method === 'DELETE')).toHaveLength(2));
    await waitFor(() => expect(screen.queryByLabelText('Run settings')).toBeNull());          // the result closes once its run is gone
  });
});
