import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ForwardTest } from './ForwardTest';

const calls: { path: string; method: string; body?: Record<string, unknown> }[] = [];
let status: Record<string, unknown>;
let tests: Record<string, unknown>[];
let detail: Record<string, unknown>;
let orders: Record<string, unknown>[];

vi.mock('../api', () => ({
  apiRequest: async (path: string, init: { method?: string; body?: string } = {}) => {
    const method = init.method ?? 'GET';
    calls.push({ path, method, body: init.body ? JSON.parse(init.body) : undefined });
    if (path === '/dhan-live/status') return status;
    if (path.startsWith('/dhan-live/orders')) return { orders };
    if (path === '/dhan-live/arm') { status = { ...status, armed: true }; return status; }
    if (path === '/dhan-live/disarm') { status = { ...status, armed: false }; return { cancelled: 2, filled_positions_still_open: ['SBIN'], status }; }
    if (path === '/dhan-live/reconcile') return { checked: 1, updated: 0, errors: 0 };
    if (path === '/forward-tests' && method === 'GET') return { forward_tests: tests, default_start_date: '2026-11-23' };
    if (path === '/forward-tests' && method === 'POST') return { id: 'f2', name: 'New', start_date: '2026-11-23', execute_live: false, excluded: [{ symbol: 'MARUTI' }] };
    if (path === '/forward-tests/f1' && method === 'GET') return detail;
    if (path.startsWith('/forward-tests/f1/') || (path === '/forward-tests/f1' && method === 'DELETE')) return {};
    if (path === '/forward-tests/f2') return { ...detail, id: 'f2', name: 'New' };
    if (path === '/signals-lab/registry') return { strategies: [{ key: 'five_ema_alert', name: '5 EMA Alert Candle' }, { key: 'mfi_dip_30m', name: 'MFI Dip' }, { key: 'psar_flip_trend_30m', name: 'PSAR Flip' }] };
    if (path === '/signals-lab/lab/universes') return { presets: [{ key: 'master', label: 'Entire Market watch list (live)', available: 341 }, { key: 'nifty50', label: 'Nifty 50', available: 12 }, { key: 'watchlist', label: 'My watchlist', available: 3 }] };
    throw new Error(`unexpected ${method} ${path}`);
  },
}));

const baseStatus = () => ({
  deployment_enabled: true, environment_block: null, armed: false, armed_at: null, can_arm: true, confirmation_phrase: 'ENABLE LIVE ORDERS',
  warning: 'Real orders on your own account. Quantity is capped at 1. Backtests of these strategies were net-negative after charges; expect small losses, not profit.',
  limits: { max_order_quantity: 1, allowed_strategies: ['five_ema_alert', 'mfi_dip_30m'], product: 'INTRADAY', segment: 'NSE_EQ', entry_window_ist: ['09:20', '14:30'], max_daily_loss_inr: '500.00', max_trades_per_day: 10, max_open_positions: 3, max_notional_inr: '50000.00' },
  today: { orders: 2, placed: 1, blocked: 1, rejected: 0, realized_pnl: '-12.50' },
});

beforeEach(() => {
  calls.length = 0; status = baseStatus(); orders = [];
  tests = [{ id: 'f1', name: 'Monday 5-EMA', strategy_key: 'five_ema_alert', status: 'active', quantity: 1, start_date: '2026-11-23', execute_live: false, instrument_count: 12, last_run_at: null, last_error: null, summary: { closed_trades: 4, net_pnl: '-310.20' } }];
  detail = { ...tests[0], timeframe: '15m', label: 'Paper', snapshot: {
    as_of: '2026-11-23T05:00:00Z', session_open: true, quantity: 1,
    universe: [{ instrument_id: 'a', symbol: 'SBIN', status: 'ok', reason: null, last_bar_close: '2026-11-23T04:59:00Z', age_minutes: 1, bars: 3000 }, { instrument_id: 'b', symbol: 'TCS', status: 'stale', reason: 'newest bar is 9 minutes old', age_minutes: 9 }],
    summary: { closed_trades: 4, wins: 1, losses: 3, win_rate: 0.25, gross_pnl: '20.00', charges: '300.00', slippage: '30.20', net_pnl: '-310.20', by_symbol: {} },
    open_positions: [{ symbol: 'SBIN', side: 'sell', qty: '1', entry_time: '2026-11-23T04:50:00Z', entry_fill: '810.20', stop_initial: '812.00', target: '804.80', mark: '809.00' }],
    trades: [{ symbol: 'TCS', side: 'sell', qty: '1', entry_time: '2026-11-23T04:00:00Z', exit_time: '2026-11-23T04:20:00Z', entry_fill: '3100.00', exit_fill: '3110.00', exit_reason: 'stop_loss', net_pnl: '-85.30', charges: '60.10' }],
    live_orders_this_pass: [{ symbol: 'SBIN', side: 'sell', status: 'blocked', reason: 'armed: live execution is not armed by the owner', order_id: null }],
  } };
});
afterEach(cleanup);

describe('ForwardTest', () => {
  it('states the hard limits, the paper-by-default rule and that these strategies lost money in backtests', async () => {
    render(<ForwardTest />);
    expect(await screen.findByText(/net-negative after charges/)).toBeTruthy();
    const limits = within(screen.getByLabelText('Hard limits'));
    expect(limits.getByText('1')).toBeTruthy();
    expect(limits.getByText(/NSE_EQ · INTRADAY only/)).toBeTruthy();
    expect(limits.getByText('five_ema_alert, mfi_dip_30m')).toBeTruthy();
    expect(limits.getByText(/09:20–14:30 IST/)).toBeTruthy();
    expect(screen.getByText(/Forward tests run a strategy on/)).toBeTruthy();
    expect(screen.getByText(/estimated realised/)).toBeTruthy();
  });

  it('explains when live execution is switched off on the deployment and does not let you arm', async () => {
    status = { ...baseStatus(), deployment_enabled: false, can_arm: false };
    render(<ForwardTest />);
    expect(await screen.findByText(/off on this deployment/)).toBeTruthy();
    expect(screen.getByText(/DHAN_LIVE_EXECUTION_ENABLED=true/)).toBeTruthy();
    expect(screen.getByText(/static IP/)).toBeTruthy();
    expect((screen.getByRole('button', { name: 'Arm live execution' }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByLabelText('Confirmation').closest('fieldset') as HTMLFieldSetElement).disabled).toBe(true);
  });

  it('arms only after the exact phrase is typed, and sends the limits as the owner set them', async () => {
    render(<ForwardTest />);
    const arm = await screen.findByRole('button', { name: 'Arm live execution' });
    expect((arm as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(screen.getByLabelText('Confirmation'), { target: { value: 'enable live orders' } });
    expect((arm as HTMLButtonElement).disabled).toBe(true);                                         // case matters
    fireEvent.change(screen.getByLabelText('Daily loss cap'), { target: { value: '300' } });
    fireEvent.change(screen.getByLabelText('Orders per day'), { target: { value: '4' } });
    fireEvent.change(screen.getByLabelText('Confirmation'), { target: { value: 'ENABLE LIVE ORDERS' } });
    expect((arm as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(arm);
    expect(await screen.findByText('ARMED')).toBeTruthy();
    const post = calls.find((c) => c.path === '/dhan-live/arm')!;
    expect(post.method).toBe('POST');
    expect(post.body).toMatchObject({ confirmation: 'ENABLE LIVE ORDERS', max_daily_loss_inr: '300', max_trades_per_day: 4, max_open_positions: 3, max_notional_inr: '50000' });
  });

  it('shows a prominent kill switch while armed, disarms, and reports cancelled orders and positions still open at the broker', async () => {
    status = { ...baseStatus(), armed: true, armed_at: '2026-11-23T03:30:00Z' };
    render(<ForwardTest />);
    const kill = await screen.findByRole('button', { name: /KILL SWITCH/ });
    fireEvent.click(kill);
    expect(await screen.findByText(/2 unfilled order\(s\) cancelled/)).toBeTruthy();
    expect(screen.getByText(/Still open at the broker: SBIN — exit them in the Dhan app/)).toBeTruthy();
    expect(calls.some((c) => c.path === '/dhan-live/disarm' && c.method === 'POST')).toBe(true);
    expect(await screen.findByRole('button', { name: 'Arm live execution' })).toBeTruthy();         // back to off
  });

  it('only allows the live flag for an allowlisted strategy at quantity 1, and creates paper tests otherwise', async () => {
    render(<ForwardTest />);
    await screen.findByRole('option', { name: /5 EMA Alert Candle · live-capable/ });
    const live = screen.getByLabelText(/Also place live orders/) as HTMLInputElement;
    expect(live.disabled).toBe(false);
    fireEvent.change(screen.getByLabelText('Quantity'), { target: { value: '5' } });
    expect(live.disabled).toBe(true);                                                               // quantity above the cap
    fireEvent.change(screen.getByLabelText('Quantity'), { target: { value: '1' } });
    fireEvent.change(screen.getByLabelText('Strategy'), { target: { value: 'psar_flip_trend_30m' } });
    expect(live.disabled).toBe(true);                                                               // not allowlisted
    fireEvent.click(screen.getByRole('button', { name: 'Create forward test' }));
    expect(await screen.findByText(/Paper only: no order is ever placed/)).toBeTruthy();
    expect(screen.getByText(/1 symbol\(s\) left out for lack of stored 1-minute data/)).toBeTruthy();
    const post = calls.find((c) => c.path === '/forward-tests' && c.method === 'POST')!;
    expect(post.body).toMatchObject({ strategy_key: 'psar_flip_trend_30m', universe: 'master', quantity: 1, start_date: '2026-11-23', execute_live: false });
  });

  it('flags a live-capable test for live orders but says nothing is placed until it is armed', async () => {
    render(<ForwardTest />);
    await screen.findByRole('option', { name: /5 EMA Alert Candle · live-capable/ });
    fireEvent.click(screen.getByLabelText(/Also place live orders/));
    fireEvent.click(screen.getByRole('button', { name: 'Create forward test' }));
    await waitFor(() => expect(calls.find((c) => c.path === '/forward-tests' && c.method === 'POST')?.body).toMatchObject({ strategy_key: 'five_ema_alert', execute_live: true }));
  });

  it('opens a test and shows data freshness, honest paper results with charges and slippage, open positions, closed trades and this pass\'s live decisions', async () => {
    render(<ForwardTest />);
    fireEvent.click(await screen.findByRole('button', { name: /Monday 5-EMA/ }));
    const panel = within(await screen.findByLabelText('Forward test detail'));
    const fresh = within(panel.getByRole('table', { name: 'Data freshness' }));
    expect(fresh.getByText('stale')).toBeTruthy();
    expect(fresh.getByText('newest bar is 9 minutes old')).toBeTruthy();                             // staleness is shown, never hidden
    const summary = within(panel.getByLabelText('Paper summary'));
    expect(summary.getByText('25.0%')).toBeTruthy();
    expect(summary.getByText('300.00')).toBeTruthy();                                               // charges
    expect(summary.getByText('30.20')).toBeTruthy();                                                // slippage
    expect(summary.getByText('-310.20').className).toContain('sl-neg');
    expect(panel.getByText('Paper results (hypothetical, after charges and slippage)')).toBeTruthy();
    expect(within(panel.getByRole('table', { name: 'Open positions' })).getByText('810.20')).toBeTruthy();
    expect(within(panel.getByRole('table', { name: 'Closed trades' })).getByText('stop_loss')).toBeTruthy();
    expect(panel.getByText(/SELL SBIN → blocked \(armed: live execution is not armed by the owner\)/)).toBeTruthy();
    expect(panel.getByText(/15m candles · quantity 1 · from 2026-11-23/)).toBeTruthy();
  });

  it('shows a waiting message before the start date and runs refresh, pause and live-flag actions against the right endpoints', async () => {
    detail = { ...detail, snapshot: { waiting: 'Starts 2026-11-23 at 09:15 IST', summary: { closed_trades: 0, wins: 0, losses: 0, win_rate: null, gross_pnl: '0', charges: '0', slippage: '0', net_pnl: '0', by_symbol: {} }, universe: [] } };
    render(<ForwardTest />);
    fireEvent.click(await screen.findByRole('button', { name: /Monday 5-EMA/ }));
    expect(await screen.findByText('Starts 2026-11-23 at 09:15 IST')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Refresh now' }));
    fireEvent.click(screen.getByRole('button', { name: 'Pause' }));
    fireEvent.click(screen.getByRole('button', { name: 'Flag for live orders' }));
    await waitFor(() => expect(calls.some((c) => c.path === '/forward-tests/f1/execute-live' && c.method === 'PUT' && (c.body as { enabled: boolean }).enabled === true)).toBe(true));
    expect(calls.some((c) => c.path === '/forward-tests/f1/refresh' && c.method === 'POST')).toBe(true);
    expect(calls.some((c) => c.path === '/forward-tests/f1/pause' && c.method === 'POST')).toBe(true);
  });

  it('deletes a test only after confirmation', async () => {
    render(<ForwardTest />);
    fireEvent.click(await screen.findByRole('button', { name: /Monday 5-EMA/ }));
    await screen.findByLabelText('Forward test detail');
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }));
    expect(calls.some((c) => c.method === 'DELETE')).toBe(false);
    fireEvent.click(within(screen.getByRole('group', { name: 'Confirm delete' })).getByRole('button', { name: 'Keep' }));
    expect(calls.some((c) => c.method === 'DELETE')).toBe(false);
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }));
    fireEvent.click(within(screen.getByRole('group', { name: 'Confirm delete' })).getByRole('button', { name: 'Delete for good' }));
    await waitFor(() => expect(calls.some((c) => c.method === 'DELETE' && c.path === '/forward-tests/f1')).toBe(true));
    await waitFor(() => expect(screen.queryByLabelText('Forward test detail')).toBeNull());
  });

  it('lists real live orders with the reason a blocked or rejected one did not go through', async () => {
    orders = [
      { id: 'o1', created_at: '2026-11-23T04:00:00Z', symbol: 'SBIN', side: 'sell', quantity: 1, strategy_key: 'five_ema_alert', status: 'rejected', block_reason: 'IP not whitelisted for order APIs', reference_price: '810.20', stop_price: '812.00', target_price: '804.80', entry_price: null, exit_price: null, exit_reason: null, realized_pnl_estimate: null, dhan_order_id: null, broker_response: {} },
      { id: 'o2', created_at: '2026-11-23T04:05:00Z', symbol: 'TCS', side: 'sell', quantity: 1, strategy_key: 'mfi_dip_30m', status: 'closed', block_reason: null, reference_price: '3100', stop_price: '3110', target_price: '3070', entry_price: '3100', exit_price: '3070', exit_reason: 'target', realized_pnl_estimate: '27.40', dhan_order_id: '9', broker_response: {} },
    ];
    render(<ForwardTest />);
    const ledger = within(await screen.findByLabelText('Live order ledger'));
    expect(await ledger.findByText('rejected')).toBeTruthy();
    expect(ledger.getByText(/IP not whitelisted for order APIs/)).toBeTruthy();
    expect(ledger.getByText('27.40').className).toContain('sl-pos');
    cleanup();
    orders = [];
    render(<ForwardTest />);
    expect(await within(await screen.findByLabelText('Live order ledger')).findByText(/No live order has been attempted/)).toBeTruthy();
  });

  it('lets the 5-EMA be forward-tested on 5-minute or 15-minute candles and sends that choice', async () => {
    render(<ForwardTest />);
    await screen.findByRole('option', { name: /5 EMA Alert Candle · live-capable/ });
    expect(screen.getByLabelText('5-EMA candle')).toBeTruthy();
    fireEvent.change(screen.getByLabelText('5-EMA candle'), { target: { value: '5' } });
    fireEvent.click(screen.getByRole('button', { name: 'Create forward test' }));
    await waitFor(() => expect(calls.find((c) => c.path === '/forward-tests' && c.method === 'POST')?.body).toMatchObject({ strategy_key: 'five_ema_alert', params: { timeframe_min: 5, vwap_filter: 1, rr: 2 } }));
    fireEvent.change(screen.getByLabelText('Strategy'), { target: { value: 'mfi_dip_30m' } });
    expect(screen.queryByLabelText('5-EMA candle')).toBeNull();                                      // only the 5-EMA has a candle choice
  });
});
