import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { CryptoLiveReport } from './CryptoLiveReport';

const { apiRequest } = vi.hoisted(() => ({ apiRequest: vi.fn() }));
vi.mock('../api', () => ({ apiRequest, ApiRequestError: class extends Error {} }));

const base = { wallet: { total: '3.87470006', available: '3.87470006', margin_in_use: '0', unrealised_pnl: '0' }, positions: [], open_orders: [], realized_pnl: { today: '-0.24494305', all_time: '0.1', all_time_note: 'n' },
  synced_at: '2026-11-25T04:00:00+00:00', stale: false, error: null, disclaimer: 'Decision support, not investment advice.',
  orders: [{ id: 'a', time: '2026-11-24T18:20:49Z', symbol: 'BTCUSD', side: 'sell', contracts: 1, leverage: 25, status: 'closed', entry_price: '59997', stop_price: '60265', target_price: '59341', block_reason: null, outcome: 'target_hit', outcome_label: 'Target hit',
    exit_at: '2026-11-25T01:32:18Z', exit_price: '59340.5', realized_pnl: '0.6565', fees: '0.0704', funding: '0.0041', net_pnl: '0.5902' },
  { id: 'b', time: '2026-11-25T02:00:16Z', symbol: 'BTCUSD', side: 'buy', contracts: 1, leverage: 25, status: 'closed', entry_price: '59225.5', stop_price: '59050', target_price: '59826', block_reason: null, outcome: 'stop_hit', outcome_label: 'Stop hit',
    exit_at: '2026-11-25T02:08:06Z', exit_price: '59015.5', realized_pnl: '-0.21', fees: '0.0349', funding: '0', net_pnl: '-0.2449' }] };

afterEach(() => { cleanup(); apiRequest.mockReset(); });

describe('CryptoLiveReport', () => {
  it('shows the wallet and each live order with its outcome', async () => {
    apiRequest.mockResolvedValue(base);
    render(<CryptoLiveReport />);
    expect(await screen.findByText('Target hit')).toBeTruthy();
    expect(screen.getByText('Stop hit')).toBeTruthy();
    expect(screen.getAllByText('$3.8747').length).toBe(2);       // total and available
    expect(screen.getByText('$0.5902')).toBeTruthy();
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('marks the data stale when Delta cannot be read', async () => {
    apiRequest.mockResolvedValue({ ...base, stale: true, error: 'wallet unreadable' });
    render(<CryptoLiveReport />);
    await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('Stale: wallet unreadable'));
  });

  it('shows a request failure as stale instead of blanking the page', async () => {
    apiRequest.mockResolvedValueOnce(base).mockRejectedValue(new Error('network down'));
    vi.useFakeTimers({ shouldAdvanceTime: true });
    render(<CryptoLiveReport />);
    await screen.findByText('Target hit');
    await vi.advanceTimersByTimeAsync(5_100);
    await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('Stale'));
    expect(screen.getByText('Target hit')).toBeTruthy();
    vi.useRealTimers();
  });
});
