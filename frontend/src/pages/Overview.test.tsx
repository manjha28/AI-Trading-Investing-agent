import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { OverviewPage } from './Overview';

const calls = vi.hoisted(() => ({ list: [] as string[] }));
vi.mock('../api', () => ({
  apiRequest: (path: string) => { calls.list.push(path); if (path.includes('/alerts')) return Promise.resolve([]); return Promise.resolve(path.startsWith('/brokers/portfolio-summary') ? { brokers: [], totals: { market_value: '1000', total_value: '1500', cash: '500', invested_cost: null, unrealized_pnl: null, day_change: null }, value_comparison: null } : path === '/brokers' ? [] : path === '/brokers/dhan/portfolio' ? { holdings: [], positions: [], closed_positions: [], delivery_sales: [], snapshot_min_interval_seconds: 60, synced_at: null, instrument_master: { last_refreshed_at: null, mapping_candidates: [], unmapped: [], mapped: 0 }, market_session: null, funds: null } : { portfolio_scoped: [], owner_wide: [], items: [], events: [], rows: [], count: 0 }); },
  ApiRequestError: class extends Error {},
}));
vi.mock('../hooks/useWatchlistStream', () => ({ useWatchlistStream: () => ({ status: 'active', message: null, quotes: {} }) }));

// Test data only: the smallest workspace the page needs.
const workspace = new Proxy({ brokerRefreshKey: 0, coverage: null, currency: 'INR', loadingPortfolio: false, portfolioId: 'p1', summary: null, thesisSymbol: null, valuation: null, valuationBySymbol: {}, watchlist: [], watchlistLoadState: 'ready', watchlistLoadError: null,
  portfolios: [], positions: [], transactions: [], riskMetrics: null }, { get: (t, k) => (k in t ? (t as Record<string, unknown>)[k as string] : () => undefined) }) as never;

afterEach(() => { cleanup(); calls.list.length = 0; });

describe('Overview broker requests', () => {
  it('makes no broker request before the session is two-factor verified, and says why', async () => {
    render(<OverviewPage workspace={workspace} brokerAccessAllowed={false} />);
    await waitFor(() => expect(screen.getByText(/Verify this session with your authenticator code/)).toBeTruthy());
    expect(calls.list.filter((p) => p.startsWith('/brokers'))).toEqual([]);
  });

  it('asks for the portfolio summary and the Dhan portfolio once each when verified, and labels the card Holdings value with a separate total incl. cash', async () => {
    render(<OverviewPage workspace={workspace} brokerAccessAllowed />);
    await waitFor(() => expect(screen.getByText('Total incl. cash')).toBeTruthy());
    expect(screen.getByText('Holdings Value')).toBeTruthy();
    expect(screen.queryByText('Portfolio Value')).toBeNull();
    expect(calls.list.filter((p) => p === '/brokers/portfolio-summary').length).toBe(1);
    expect(calls.list.filter((p) => p === '/brokers/dhan/portfolio').length).toBe(1);
  });
});
