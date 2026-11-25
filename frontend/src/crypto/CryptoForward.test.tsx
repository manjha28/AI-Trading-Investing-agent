import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { CryptoForward } from './CryptoForward';

const { apiRequest } = vi.hoisted(() => ({ apiRequest: vi.fn() }));
vi.mock('../api', () => ({ apiRequest, ApiRequestError: class extends Error {} }));

const summary = { closed_trades: 2, wins: 1, losses: 1, win_rate: 0.5, gross_pnl: '10', fees: '1', slippage: '0.5', net_pnl: '8.5', avg_r: '0.4' };
const test = { id: 'a', name: '5 EMA BTCUSD 15m', symbol: 'BTCUSD', resolution: '15m', status: 'active', contracts: 1, start_time: '2026-11-23T10:00:00Z', last_run_at: null, last_error: null,
  label: 'x', summary, open_positions: [], trades: [], params: { ema: 5, rr: '3', direction: -1 } };

describe('CryptoForward', () => {
  afterEach(() => { cleanup(); apiRequest.mockReset(); });

  it('lists paper tests, labels them hypothetical and shows unknown win rate as a dash', async () => {
    apiRequest.mockResolvedValue({ tests: [test, { ...test, id: 'b', name: 'empty', summary: { ...summary, closed_trades: 0, win_rate: null, net_pnl: '0' } }] });
    render(<CryptoForward symbol="BTCUSD" resolution="15m" part="tests" />);
    expect(await screen.findByText('5 EMA BTCUSD 15m')).toBeTruthy();
    expect(screen.getByLabelText('About paper tests').getAttribute('title')).toMatch(/never mixes with real P&L/);
    expect(screen.getByText(/win 50%/)).toBeTruthy();
    expect(screen.getAllByText(/win —/).length).toBeGreaterThan(0);
  });

  it('starts a test on the selected contract and shows a failure plainly', async () => {
    apiRequest.mockImplementation((path: string, init?: { method?: string }) => {
      if (init?.method === 'POST') return Promise.reject(new Error('BTCUSD is not a perpetual contract.'));
      return Promise.resolve({ tests: [] });
    });
    render(<CryptoForward symbol="BTCUSD" resolution="15m" part="tests" />);
    fireEvent.click(await screen.findByText('New paper test…'));
    fireEvent.click(await screen.findByText('Start paper test'));
    await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('not a perpetual contract'));
    const post = apiRequest.mock.calls.find((c) => c[1]?.method === 'POST');
    expect(JSON.parse(post![1].body)).toMatchObject({ symbol: 'BTCUSD', resolution: '15m', direction: -1 });
  });
});

const venue = (flag: boolean, armed: boolean, reasons: string[], state: string, today = { sent_to_broker: 0, rejected_by_broker: 0, blocked_attempts: 0 }) => ({ venue: 'delta', label: 'Delta Exchange India (USD perpetuals)', deployment_flag: { name: 'DELTA_LIVE_EXECUTION_ENABLED', enabled: flag }, armed, armed_at: null,
  window: { kind: '24x7', text: '24 hours, 7 days', open_now: true }, strategies: [{ key: 'five_ema_alert', live_tests: 0 }], live_tests: 0, effective: { state, reasons }, limits: {}, today });
const limits = { max_contracts: 1, leverage: "min(25x, the contract's exchange maximum)", allowed_strategies: ['five_ema_alert'], max_daily_loss_usd: '25', max_trades_per_day: 6, max_open_positions: 2, max_notional_usd: '2000' };
const status = (flag: boolean, armed: boolean, state: string, reasons: string[], today?: { sent_to_broker: number; rejected_by_broker: number; blocked_attempts: number }) => ({ deployment_enabled: flag, credentials_present: true, armed, can_arm: flag, confirmation_phrase: 'ENABLE DELTA LIVE ORDERS', warning: 'x', limits, execution: venue(flag, armed, reasons, state, today) });
const serve = (st: unknown, orders: unknown = { orders: [], blocked_attempts: [] }, tests: unknown[] = []) => apiRequest.mockImplementation((path: string) => Promise.resolve(path.endsWith('/live/status') ? st : path.includes('/live/orders') ? orders : { tests }));

describe('CryptoForward live controls', () => {
  afterEach(() => { cleanup(); apiRequest.mockReset(); });

  it('OFF with the flag off: states the reason from the backend and offers no live control at all', async () => {
    serve(status(false, false, 'OFF', ['the deployment flag DELTA_LIVE_EXECUTION_ENABLED is off']));
    render(<CryptoForward symbol="BTCUSD" resolution="15m" part="live" />);
    expect(await screen.findByText(/the deployment flag DELTA_LIVE_EXECUTION_ENABLED is off/)).toBeTruthy();
    expect(screen.queryByLabelText('Confirmation phrase')).toBeNull(); expect(screen.queryByText(/Kill switch/)).toBeNull();
    expect(screen.queryByText(/Live: o(n|ff)/)).toBeNull();
  });

  it('flag on but not armed: turning live on needs no typing, only a warning step that names the venue, strategy, quantity cap and leverage', async () => {
    serve(status(true, false, 'OFF', ['you have not armed live execution']));
    render(<CryptoForward symbol="BTCUSD" resolution="15m" part="live" />);
    const arm = await screen.findByRole('button', { name: 'Turn live orders on…' });
    expect(screen.queryByLabelText('Confirmation phrase')).toBeNull();
    fireEvent.click(arm);
    const dialog = await screen.findByRole('dialog', { name: /Turn live orders on for Delta Exchange India/ });
    expect(within(dialog).getByText(/five_ema_alert/)).toBeTruthy(); expect(within(dialog).getByText(/1 contract per order/)).toBeTruthy(); expect(within(dialog).getByText(/min\(25x/)).toBeTruthy();
    expect(apiRequest.mock.calls.some((c) => String(c[0]).endsWith('/live/arm'))).toBe(false);          // nothing is armed until the confirmation
  });

  it('armed: shows the kill switch and the per-test Live switch; today never merges blocked attempts into sent orders', async () => {
    serve(status(true, true, 'ARMED_BUT_BLOCKED', ['no Delta forward test has its live switch on'], { sent_to_broker: 1, rejected_by_broker: 1, blocked_attempts: 2 }),
      { orders: [{ id: 'o1', created_at: '2026-11-23T19:50:17Z', symbol: 'BTCUSD', side: 'buy', contracts: 1, leverage: 25, status: 'rejected', block_reason: 'insufficient_margin', delta_order_id: null }],
        blocked_attempts: [{ id: 'b1', created_at: '2026-11-23T19:00:00Z', symbol: 'ETHUSD', side: 'sell', contracts: 1, block_reason: 'signal_fresh: stale' }] },
      [{ id: 't1', name: '5 EMA BTCUSD 5m', symbol: 'BTCUSD', resolution: '5m', status: 'active', contracts: 1, start_time: '2026-11-23T10:00:00Z', last_run_at: null, last_error: null, execute_live: false, label: 'x', summary: null, open_positions: [], params: { ema: 5, rr: '3', direction: -1 } }]);
    render(<CryptoForward symbol="BTCUSD" resolution="15m" part="live" />);
    expect(await screen.findByText(/Kill switch/)).toBeTruthy();
    expect(screen.getByLabelText("Limits and today's activity").getAttribute('title')).toContain('Today: 1 sent to Delta (1 rejected by the broker) · 2 blocked attempts (never sent)');
    expect(screen.queryByText(/Orders off/i)).toBeNull();
    expect(screen.queryByText('ETHUSD')).toBeNull();                       // blocked attempts are collapsed to one line
    fireEvent.click(screen.getByRole('button', { name: 'view' }));
    const row = screen.getByText('ETHUSD').closest('tr') as HTMLElement;
    expect(within(row).getByText('Signal stale').getAttribute('title')).toContain('signal_fresh');
    cleanup();
    serve(status(true, true, 'ARMED_BUT_BLOCKED', ['no Delta forward test has its live switch on']), { orders: [], blocked_attempts: [] },
      [{ id: 't1', name: '5 EMA BTCUSD 5m', symbol: 'BTCUSD', resolution: '5m', status: 'active', contracts: 1, start_time: '2026-11-23T10:00:00Z', last_run_at: null, last_error: null, execute_live: false, label: 'x', summary: null, open_positions: [], params: { ema: 5, rr: '3', direction: -1 } }]);
    const t1 = { id: 't1', name: '5 EMA BTCUSD 5m', symbol: 'BTCUSD', resolution: '5m', status: 'active', contracts: 1, start_time: '2026-11-23T10:00:00Z', last_run_at: null, last_error: null, execute_live: false, label: 'x', summary: null, open_positions: [], trades: [], params: { ema: 5, rr: '3', direction: -1 } };
    const base = apiRequest.getMockImplementation()!;
    apiRequest.mockImplementation((path: string) => (path.endsWith('/forward-tests/t1') ? Promise.resolve(t1) : base(path)));
    render(<CryptoForward symbol="BTCUSD" resolution="15m" part="tests" />);
    fireEvent.click(await screen.findByText('5 EMA BTCUSD 5m'));
    expect(await screen.findByRole('button', { name: 'Live: off' })).toBeTruthy();
  });
});
