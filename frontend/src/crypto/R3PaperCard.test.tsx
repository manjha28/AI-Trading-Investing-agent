import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { R3PaperCard } from './R3PaperCard';

const apiRequest = vi.fn();
vi.mock('../api', () => ({ apiRequest: (...a: unknown[]) => apiRequest(...a) }));
afterEach(() => { cleanup(); apiRequest.mockReset(); });

describe('R3PaperCard', () => {
  it('shows the lab status and says when no trade has happened yet (TEST DATA)', async () => {
    apiRequest.mockResolvedValue({ stale: false, error: null, fetched_at: '2026-11-25T08:00:00Z', started: '2026-11-24T07:48:00+00:00', param_hash: 'eaea623c741f', paper_heartbeat_age_s: 5,
      recorder: { running: true, uptime_pct_24h: 99.9, last_snapshot: null }, symbols: { BTCUSD: { open: 0, closed: 0, last_bar: null } }, trades: [], closed_trades: 0, net_usd_total: '0',
      checkpoints: { trade30: null, holdout_reference_net_bps: 45.3 }, go_no_go: { decided: false, reason: '0 of 50 combined trades so far' }, label: 'Paper only.' });
    render(<R3PaperCard />);
    expect(await screen.findByText(/No paper trade yet/)).toBeTruthy();
    expect(screen.getByText('0 of 50')).toBeTruthy();
    expect(screen.getByText('alive')).toBeTruthy();
  });

  it('marks a lab that cannot be reached as stale instead of showing zeros', async () => {
    apiRequest.mockResolvedValue({ stale: true, error: 'the lab machine could not be reached (ConnectError)', fetched_at: null });
    render(<R3PaperCard />);
    expect((await screen.findByRole('alert')).textContent).toMatch(/could not be reached/);
    expect(screen.queryByText('0 of 50')).toBeNull();
  });
});
