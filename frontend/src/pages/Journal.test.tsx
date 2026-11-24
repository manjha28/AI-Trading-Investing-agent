import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { JournalPage } from './Journal';

vi.mock('../api', () => ({ apiRequest: () => Promise.resolve({ strategies: [], setups: [], tags: [] }), ApiRequestError: class extends Error {}, downloadCsv: vi.fn() }));
vi.mock('../components/DhanReconciliation', () => ({ DhanReconciliation: () => null }));

// TEST DATA: three closed trades with different net P&L, entered in a different order.
const trade = (id: string, instrument: string, net: string, entry: string, over: object = {}) => ({ id, annotation_id: id, instrument, asset_class: 'equity', currency: 'INR', side: 'long', source_side: null, quantity: '10', multiplier: '1', entry_price: '100', exit_price: '101', entry_time: entry,
  exit_time: entry, fees: '1', net_pnl: net, strategy: null, setup: null, notes: null, tags: [], source: 'manual', match_state: 'matched', position_status: 'closed', calendar_date: entry.slice(0, 10), entry_fill_id: null, exit_fill_id: null, ...over });
const trades = [trade('a', 'AAA', '10.00', '2026-11-20T10:00:00Z'), trade('b', 'BBB', '-5.00', '2026-11-21T10:00:00Z'), trade('c', 'CCC', '30.00', '2026-11-19T10:00:00Z', { strategy: 'fast_slow_ma' }), trade('d', 'DDD', '2.00', '2026-11-18T10:00:00Z', { strategy: 'five_ema_alert' })];

const workspace = (over: object = {}) => new Proxy({ currency: 'INR', editingTrade: null, journalAnalytics: null, journalAssetClass: '', journalBreakdown: [], journalCurrency: 'INR', journalHasExit: '', journalTrades: trades, journalLoadState: 'loaded', journalLoadError: '', ...over },
  { get: (t, k) => (k in t ? (t as Record<string, unknown>)[k as string] : () => Promise.resolve()) }) as never;
const order = () => [...document.querySelectorAll('.journal-table tbody tr')].map((r) => r.querySelector('td')?.textContent?.slice(0, 3));
afterEach(cleanup);

describe('Journal sorting', () => {
  it('re-orders the rows when the Net P&L header is clicked, ascending then descending', () => {
    render(<JournalPage workspace={workspace()} />);
    expect(order()).toEqual(['BBB', 'AAA', 'CCC', 'DDD']);                       // default: newest entry first
    const header = screen.getByRole('button', { name: /Net P&L/ });
    fireEvent.click(header);
    expect(order()).toEqual(['BBB', 'DDD', 'AAA', 'CCC']);                       // ascending: -5, 2, 10, 30
    fireEvent.click(header);
    expect(order()).toEqual(['CCC', 'AAA', 'DDD', 'BBB']);                       // descending
  });
});

describe('Journal currency and filter options', () => {
  it('says "No USD trades" (and what currency the trades are in) instead of an empty table', () => {
    render(<JournalPage workspace={workspace({ journalCurrency: 'USD' })} />);
    expect(screen.getByText(/No USD trades\. Your recorded trades are in INR\./)).toBeTruthy();
    expect(document.querySelector('.journal-table')).toBeNull();
  });
  it('lists only strategies that have trades, each with its count', () => {
    render(<JournalPage workspace={workspace()} />);
    const select = screen.getByLabelText('Strategy') as HTMLSelectElement;
    expect([...select.options].map((o) => o.textContent)).toEqual(['All strategies', 'fast_slow_ma (1)', 'five_ema_alert (1)']);
    expect(within(select).queryByText(/sma_rsi/)).toBeNull();
  });
});
