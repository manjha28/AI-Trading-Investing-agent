import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
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
    render(<CryptoForward symbol="BTCUSD" resolution="15m" />);
    expect(await screen.findByText('5 EMA BTCUSD 15m')).toBeTruthy();
    expect(screen.getByText(/places no order/)).toBeTruthy();
    expect(screen.getByText('50.0%')).toBeTruthy();
    expect(screen.getAllByText('—').length).toBeGreaterThan(0);
  });

  it('starts a test on the selected contract and shows a failure plainly', async () => {
    apiRequest.mockImplementation((path: string, init?: { method?: string }) => {
      if (init?.method === 'POST') return Promise.reject(new Error('BTCUSD is not a perpetual contract.'));
      return Promise.resolve({ tests: [] });
    });
    render(<CryptoForward symbol="BTCUSD" resolution="15m" />);
    fireEvent.click(await screen.findByText('Start paper test'));
    await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('not a perpetual contract'));
    const post = apiRequest.mock.calls.find((c) => c[1]?.method === 'POST');
    expect(JSON.parse(post![1].body)).toMatchObject({ symbol: 'BTCUSD', resolution: '15m', direction: -1 });
  });
});
