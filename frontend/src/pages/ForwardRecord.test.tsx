import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ForwardRecordPage } from './ForwardRecord';

const mockApi = vi.hoisted(() => vi.fn());
vi.mock('../api', () => ({ apiRequest: mockApi }));
afterEach(() => { cleanup(); mockApi.mockReset(); });

const stats = (trades: number) => ({ trades, t1_hits: 2, hit_rate: trades ? 0.4 : null, hit_ci95: trades ? [0.1, 0.7] : [null, null], net_r: trades ? 0.25 : null, net_r_ci95: trades ? [-0.2, 0.7] : [null, null], stopped: 1, timed_out: 0 });
const block = (n: number) => ({ all: stats(n), by_verdict: { buy_now: stats(n), buy_on_trigger: stats(0) }, blue_sky: stats(0), not_blue_sky: stats(n), reliability: [] });

describe('ForwardRecordPage', () => {
  it('says when no report is stored yet', async () => {
    mockApi.mockResolvedValue({ available: false, message: 'No weekly forward-record report has been stored yet', disclaimer: 'Decision support, not investment advice.' });
    render(<ForwardRecordPage />);
    await waitFor(() => expect(screen.getByText(/No weekly forward-record report/)).toBeTruthy());
  });
  it('shows hit rate, net R with its interval, the blue-sky line and the long-term wait note; unknown is a dash, never zero', async () => {
    mockApi.mockResolvedValue({ available: true, as_of: '2026-12-01', computed_at: '2026-12-01T10:00:00+00:00', disclaimer: 'Decision support, not investment advice.',
      short_horizon: { observations: 9, first_signal: '2026-11-22', not_a_trade: { unfilled: 2 }, by_horizon: { swing: { episodes: block(5), all_nights: block(9) }, positional: { episodes: block(0), all_nights: block(0) } } },
      long_term: { available: true, first_class_date: '2026-11-22', benchmark: 'NIFTY 500 price index', by_months: { '3': { note: 'No class observation is 3 months old yet' } } } });
    render(<ForwardRecordPage />);
    await waitFor(() => expect(screen.getByText('Swing (15 sessions)')).toBeTruthy());
    expect(screen.getAllByText(/Blue-sky ladders/).length).toBe(2);
    expect(screen.getAllByText(/40% \[10%, 70%\]/, { selector: 'td' }).length).toBeGreaterThan(0);
    expect(screen.getByText(/No class observation is 3 months old yet/)).toBeTruthy();
    expect(screen.getAllByText('—').length).toBeGreaterThan(0);
  });
});
