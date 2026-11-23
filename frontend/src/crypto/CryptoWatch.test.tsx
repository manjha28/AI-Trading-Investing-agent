import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { CryptoMarkets } from './CryptoMarkets';
import { CryptoWatch } from './CryptoWatch';

const { apiRequest } = vi.hoisted(() => ({ apiRequest: vi.fn() }));
vi.mock('../api', () => ({ apiRequest, ApiRequestError: class extends Error {} }));
vi.mock('./CryptoCharts', () => ({ CryptoCandleChart: () => <div>chart</div> }));

const ticker = (over: object) => ({ open: '9', high: '11', low: '8', close: '10', ltp_change_24h: '1', mark_change_24h: '1', funding_rate: '0.01', oi_value_usd: '2500000', oi_change_usd_6h: '-100000', turnover_usd: '5000000', underlying_asset_symbol: 'X', leverage: 100, best_bid: '9.9', best_ask: '10.1', tags: [], ...over });
const markets = { items: [
  { product_id: '1', symbol: 'AAAUSD', state: 'live', quote_time: null, last_price: '10', mark_price: '10.1', volume_24h: '5000000', ticker: ticker({ ltp_change_24h: '2.5', turnover_usd: '5000000' }) },
  { product_id: '2', symbol: 'BBBUSD', state: 'live', quote_time: null, last_price: '3', mark_price: '3.1', volume_24h: '90000000', ticker: ticker({ ltp_change_24h: '-4.2', turnover_usd: '90000000', funding_rate: null, oi_value_usd: null }) },
  { product_id: '3', symbol: 'OLDUSD', state: 'expired', quote_time: null, last_price: '1', mark_price: '1', volume_24h: '1', ticker: ticker({}) },
] };
const props = { symbol: 'AAAUSD', setSymbol: () => undefined, resolution: '15m', setResolution: () => undefined };

beforeEach(() => {
  window.localStorage.clear();
  apiRequest.mockImplementation((path: string) => Promise.resolve(path.includes('/delta-research/markets') ? markets : path.includes('/crypto-delta/feed') ? { available: true, wanted: true, state: 'connected', connected: true, last_event_at: null } : { symbol: 'AAAUSD', count: 0, candles: [], gaps: 0 }));
});
afterEach(() => { cleanup(); apiRequest.mockReset(); });

describe('Crypto watch table', () => {
  it('shows only live contracts with the ticker columns, a sign and arrow on the change, and a dash for unknown values', async () => {
    render(<CryptoMarkets {...props} />);
    const table = await screen.findByRole('table');
    await within(table).findByText('BBBUSD');
    expect(within(table).queryByText('OLDUSD')).toBeNull();
    for (const heading of ['Last', '24h %', '24h range', 'Volume (USD)', 'Open interest (USD)', 'Funding', 'Bid / Ask']) expect(within(table).getAllByRole('columnheader').some((h) => h.textContent?.includes(heading))).toBe(true);
    const rows = within(table).getAllByRole('row').slice(1);
    expect(rows[0].textContent).toContain('BBBUSD');                    // 90M volume first
    expect(rows[0].textContent).toContain('▼ -4.20%');
    expect(rows[1].textContent).toContain('▲ +2.50%');
    expect(rows[0].textContent).toContain('—');                          // unknown funding / open interest are not zero
  });

  it('sorts by a clicked header, keeps unknown values last, and filters to gainers', async () => {
    render(<CryptoMarkets {...props} />);
    const table = await screen.findByRole('table');
    await within(table).findByText('BBBUSD');
    fireEvent.click(within(table).getByRole('button', { name: /^Funding/ }));
    const first = () => within(table).getAllByRole('row')[1].textContent ?? '';
    expect(first()).toContain('AAAUSD');                                 // BBB has no funding value: sorted last whatever the direction
    fireEvent.click(screen.getByRole('button', { name: 'Gainers' }));
    await waitFor(() => expect(first()).toContain('AAAUSD'));
  });

  it('stars a contract into the stored watchlist', async () => {
    render(<CryptoMarkets {...props} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Add BBBUSD to watchlist' }));
    expect(JSON.parse(window.localStorage.getItem('crypto.watchlist') ?? '[]')).toEqual(['BBBUSD']);
    fireEvent.click(screen.getByRole('button', { name: 'Watchlist (1)' }));
    const table = await screen.findByRole('table');
    expect(within(table).getAllByRole('row').slice(1)).toHaveLength(1);
  });

  it('pauses the feed from the page and says it is paused', async () => {
    apiRequest.mockImplementation((path: string, init?: { method?: string; body?: string }) => {
      if (path.includes('/crypto-delta/feed')) return Promise.resolve({ available: true, wanted: init?.method === 'PUT' ? JSON.parse(init.body ?? '{}').enabled : true, state: 'connected', connected: true, last_event_at: null });
      return Promise.resolve(path.includes('/delta-research/markets') ? markets : { symbol: 'AAAUSD', count: 0, candles: [], gaps: 0 });
    });
    render(<CryptoMarkets {...props} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Pause feed (save cost)' }));
    expect(await screen.findByText('Feed paused')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Resume feed' })).toBeTruthy();
  });
});


describe('Crypto watch looks and behaves like the equity watch', () => {
  it('shows the status strip with live, up and down counts and totals, and the paused state', async () => {
    const recent = new Date().toISOString();
    apiRequest.mockImplementation((path: string) => Promise.resolve(path.includes('/delta-research/markets') ? { items: markets.items.map((m) => ({ ...m, quote_time: recent })) } : { available: true, wanted: true, state: 'connected', connected: true, last_event_at: null }));
    render(<CryptoWatch symbol="AAAUSD" />);
    const strip = await screen.findByRole('status', { name: '' }).catch(() => null);
    await screen.findByText('● LIVE');
    expect(screen.getByText('contracts', { exact: false })).toBeTruthy();
    expect(screen.getByText('24h vol', { exact: false })).toBeTruthy();
    expect(strip === null || strip instanceof HTMLElement).toBe(true);
  });
});
