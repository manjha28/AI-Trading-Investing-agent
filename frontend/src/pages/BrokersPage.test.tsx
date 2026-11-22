import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { BrokersPage } from './BrokersPage';

const broker = (slug: string, name: string, extra: Record<string, unknown> = {}) => ({ slug, name, enabled: true, synthetic: false, connected: false, connection_status: 'not_connected', integration_status: 'setup_required', expires_at: null, last_synced_at: null, ...extra });
const brokers = [broker('mock', 'Mock Broker', { synthetic: true, integration_status: 'synthetic', enabled: true }), broker('zerodha', 'Zerodha'), broker('upstox', 'Upstox'),
  broker('dhan', 'Dhan', { connected: true, connection_status: 'connected', integration_status: 'available', last_synced_at: '2026-11-22T08:00:00Z', expires_at: '2026-11-23T08:00:00Z' })];
vi.mock('../api', () => ({
  ApiRequestError: class extends Error {},
  apiRequest: (path: string) => Promise.resolve(path === '/brokers' ? brokers : path === '/market-data/settings' ? { selected_provider: null, sources: [{ name: 'angel_one', label: 'Angel One', available: false, status: 'session_expired', expires_at: null }] }
    : path.includes('/account') ? { holdings: [], positions: [], funds: null, profile: null, order_history: [], trades: [], ledger: [] } : path.includes('reconciliation') ? { differences: [] } : {}),
}));
afterEach(cleanup);

describe('BrokersPage', () => {
  it('shows the connected broker first, hides the mock broker, and collapses unconfigured and expired brokers under Other brokers', async () => {
    render(<BrokersPage portfolioId="p1" />);
    await waitFor(() => expect(screen.getByRole('heading', { name: 'Dhan' })).toBeTruthy());
    const headings = screen.getAllByRole('heading', { level: 2 }).map((h) => h.textContent);
    expect(headings[0]).toBe('Dhan');
    expect(screen.queryByRole('heading', { name: 'Mock Broker' })).toBeNull();
    const others = screen.getByText(/Other brokers \(\d+\)/).closest('details')!;
    expect(others.hasAttribute('open')).toBe(false);
    expect(within(others).getByRole('heading', { name: 'Zerodha' })).toBeTruthy();
    expect(within(others).getByRole('heading', { name: 'Angel One SmartAPI' })).toBeTruthy();       // session expired: not a primary card
    expect(screen.getByText(/the only order path in the app is Signals > Forward Test/)).toBeTruthy();
  });
});
