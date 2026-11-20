import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { CommodityWatch, mcxSessionOpen } from './CommodityWatch';

const stream = vi.hoisted(() => ({ value: { status: 'active', message: null as string | null, ticks: {} as Record<string, unknown> } }));
vi.mock('../hooks/useCommodityStream', () => ({ useCommodityStream: () => stream.value }));

const contracts = {
  count: 3, exchange: 'MCX', hours_ist: '09:00–23:30 Mon–Fri', session_open: true, disclaimer: 'x',
  instruments: [
    { key: 'mcx:1', security_id: '1', symbol: 'CRUDEOIL', name: 'Crude Oil future', contract: 'CRUDEOIL', expiry: '2026-12-18', days_to_expiry: 28, lot_size: '1', tick_size: '100.0' },
    { key: 'mcx:2', security_id: '2', symbol: 'GOLD', name: 'Gold future', contract: 'GOLD', expiry: '2026-12-04', days_to_expiry: 14, lot_size: '1', tick_size: '100.0' },
    { key: 'mcx:3', security_id: '3', symbol: 'SILVER', name: null, contract: 'SILVER', expiry: '2026-12-04', days_to_expiry: 14, lot_size: null, tick_size: null },
  ],
};
const tick = (key: string, symbol: string, over: Record<string, string | number | null> = {}) => ({
  key, symbol, ltp: '122162', previous_close: '127452', change: '-5290', change_pct: '-4.15', open: '123629', high: '123975', low: '121424',
  volume: '13234', received_at: '2026-11-20T15:20:00Z', receivedAt: OPEN_TUESDAY - 5_000, ...over,
});

// Tuesday 2026-11-17 12:00 IST (06:30 UTC): MCX open. Only Date is faked so async helpers keep working.
const OPEN_TUESDAY = Date.UTC(2026, 10, 17, 6, 30);

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['Date'], now: OPEN_TUESDAY });
  stream.value = { status: 'active', message: null, ticks: {} };
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify(contracts), { status: 200, headers: { 'Content-Type': 'application/json' } })));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.useRealTimers(); });

// Indian digit grouping (lakhs), the same locale the page uses
const fmt = (n: number) => n.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const compact = (n: number) => new Intl.NumberFormat('en-IN', { notation: 'compact', maximumFractionDigits: 2 }).format(n);
const rowOf = (symbol: string) => screen.getAllByText(symbol)[0].closest('tr')!;

describe('CommodityWatch', () => {
  it('lists the front-month contracts with the exchange hours, and shows unknown prices as dashes before any tick', async () => {
    render(<CommodityWatch />);
    await screen.findByText('GOLD');
    expect(screen.getByText(/09:00–23:30 Mon–Fri/)).toBeTruthy();
    const gold = rowOf('GOLD');
    expect(within(gold).getByText('2026-12-04')).toBeTruthy();
    expect(within(gold).getByText('14d')).toBeTruthy();
    expect(within(gold).getAllByText('—').length).toBeGreaterThanOrEqual(4); // LTP, change, change %, volume... unknown, never 0
    expect(screen.getByText('● LIVE')).toBeTruthy();
    expect(document.querySelector('.watch-strip')?.textContent).toMatch(/3 contracts.*0 live/);
  });

  it('shows live prices with change against the reference close, and counts advancers and decliners', async () => {
    stream.value = { status: 'active', message: null, ticks: {
      'mcx:2': tick('mcx:2', 'GOLD'),
      'mcx:1': tick('mcx:1', 'CRUDEOIL', { ltp: '7581', previous_close: '7500', change: '81', change_pct: '1.08', open: '7600', high: '7650', low: '7500', volume: '26119' }),
      'mcx:3': tick('mcx:3', 'SILVER', { previous_close: null, change: null, change_pct: null }),     // reference close unavailable
    } };
    render(<CommodityWatch />);
    await screen.findByText('GOLD');
    const gold = rowOf('GOLD');
    expect(within(gold).getByText(fmt(122162))).toBeTruthy();
    expect(within(gold).getByText(fmt(-5290))).toBeTruthy();
    expect(within(gold).getByText('-4.15%')).toBeTruthy();
    expect(within(gold).getByText(fmt(121424))).toBeTruthy();   // day low
    expect(within(gold).getByText(fmt(123975))).toBeTruthy();   // day high
    const crude = rowOf('CRUDEOIL');
    expect(within(crude).getByText('+1.08%')).toBeTruthy();
    expect(within(crude).getByText(compact(26119))).toBeTruthy();
    const silver = rowOf('SILVER');
    expect(within(silver).getByText(fmt(122162))).toBeTruthy(); // its live price shows even though its change is unknown
    expect(within(silver).getAllByText('—').length).toBeGreaterThanOrEqual(2); // change unknown, shown as "—" not 0.00
    // 3 contracts, all 3 live; one advancer (crude), one decliner (gold); silver's unknown change counts as neither
    expect(document.querySelector('.watch-strip')?.textContent).toMatch(/3 contracts.*3 live.*▲ 1.*▼ 1/);
  });

  it('shows the selected contract\'s day statistics and says there is no chart yet', async () => {
    stream.value = { status: 'active', message: null, ticks: { 'mcx:2': tick('mcx:2', 'GOLD') } };
    render(<CommodityWatch />);
    fireEvent.click((await screen.findByText('GOLD')).closest('tr')!);
    const panel = screen.getByRole('complementary', { name: 'Commodity detail' });
    expect(within(panel).getByText(fmt(127452))).toBeTruthy();   // previous close
    expect(within(panel).getByText(fmt(123629))).toBeTruthy();   // open
    expect(within(panel).getByText('2026-12-04 (14d)')).toBeTruthy();
    expect(within(panel).getByText(/no chart/)).toBeTruthy();
  });

  it('labels a closed market from the clock even if the stream still says active, and a refused feed plainly', async () => {
    vi.setSystemTime(Date.UTC(2026, 10, 20, 18, 5));   // Fri 23:35 IST: five minutes after the close
    stream.value = { status: 'active', message: null, ticks: { 'mcx:2': tick('mcx:2', 'GOLD', { receivedAt: Date.UTC(2026, 10, 20, 17, 59) }) } };
    const { unmount } = render(<CommodityWatch />);
    await screen.findByText('GOLD');
    expect(screen.getByText('● MCX CLOSED')).toBeTruthy();
    expect(screen.queryByText('● LIVE')).toBeNull();
    expect(screen.getByText(/MCX is closed \(09:00–23:30 IST, Mon–Fri\)/)).toBeTruthy();
    expect(rowOf('GOLD').querySelector('.watch-dot.live')).toBeNull();        // no live dot after the close
    expect(document.querySelector('.watch-strip')?.textContent).toMatch(/1 with prices/);
    unmount();
    vi.setSystemTime(OPEN_TUESDAY);
    stream.value = { status: 'unavailable', message: 'Verify this session before using broker market data.', ticks: {} };
    render(<CommodityWatch />);
    expect(await screen.findByText('Verify this session before using broker market data.')).toBeTruthy();
    expect(screen.getByText('● FEED UNAVAILABLE')).toBeTruthy();
  });

  it('says there are no recent ticks instead of LIVE when the session is open but the feed has gone quiet', async () => {
    stream.value = { status: 'active', message: null, ticks: { 'mcx:2': tick('mcx:2', 'GOLD', { receivedAt: OPEN_TUESDAY - 5 * 60_000 }) } };
    render(<CommodityWatch />);
    await screen.findByText('GOLD');
    expect(screen.getByText('● NO RECENT TICKS')).toBeTruthy();
    expect(screen.queryByText('● LIVE')).toBeNull();
    expect(rowOf('GOLD').querySelector('.watch-dot.live')).toBeNull();
  });

  it('flashes a price that changes', async () => {
    stream.value = { status: 'active', message: null, ticks: { 'mcx:2': tick('mcx:2', 'GOLD') } };
    const { rerender } = render(<CommodityWatch />);
    await screen.findByText('GOLD');
    stream.value = { status: 'active', message: null, ticks: { 'mcx:2': tick('mcx:2', 'GOLD', { ltp: '122210' }) } };
    await act(async () => { rerender(<CommodityWatch />); });
    expect(within(rowOf('GOLD')).getByText(fmt(122210)).className).toContain('flash-up');
    stream.value = { status: 'active', message: null, ticks: { 'mcx:2': tick('mcx:2', 'GOLD', { ltp: '122100' }) } };
    await act(async () => { rerender(<CommodityWatch />); });
    expect(within(rowOf('GOLD')).getByText(fmt(122100)).className).toContain('flash-down');
  });
});

describe('CommodityWatch ordering', () => {
  it('keeps contracts with no data at the bottom, alphabetical within each group', async () => {
    stream.value = { status: 'active', message: null, ticks: { 'mcx:2': tick('mcx:2', 'GOLD'), 'mcx:3': tick('mcx:3', 'SILVER') } };
    render(<CommodityWatch />);
    await screen.findByText('GOLD');
    const order = Array.from(document.querySelectorAll('.watch-table tbody tr strong')).map((n) => n.textContent);
    expect(order).toEqual(['GOLD', 'SILVER', 'CRUDEOIL']); // CRUDEOIL has no tick, though it sorts first alphabetically
    expect(rowOf('CRUDEOIL').getAttribute('title')).toBe('No trade yet — price unknown');
  });
});

describe('CommodityWatch sorting', () => {
  const order = () => Array.from(document.querySelectorAll('.watch-table tbody tr strong')).map((n) => n.textContent);
  it('sorts by Chg % both ways with no-data contracts always last', async () => {
    stream.value = { status: 'active', message: null, ticks: {
      'mcx:2': tick('mcx:2', 'GOLD', { change_pct: '-4.15' }),
      'mcx:3': tick('mcx:3', 'SILVER', { change_pct: '2.50' }),
    } };   // CRUDEOIL has no tick
    render(<CommodityWatch />);
    await screen.findByText('GOLD');
    fireEvent.click(screen.getByRole('button', { name: /Chg %/ }));          // first click: descending
    expect(order()).toEqual(['SILVER', 'GOLD', 'CRUDEOIL']);
    fireEvent.click(screen.getByRole('button', { name: /Chg %/ }));          // second: ascending, unknown still last
    expect(order()).toEqual(['GOLD', 'SILVER', 'CRUDEOIL']);
  });
  it('puts a priced contract whose change is unknown after the ones with a change', async () => {
    stream.value = { status: 'active', message: null, ticks: {
      'mcx:2': tick('mcx:2', 'GOLD', { change_pct: null, change: null }),
      'mcx:3': tick('mcx:3', 'SILVER', { change_pct: '-1.00' }),
    } };
    render(<CommodityWatch />);
    await screen.findByText('GOLD');
    fireEvent.click(screen.getByRole('button', { name: /Chg %/ }));
    expect(order().slice(0, 2)).toEqual(['SILVER', 'GOLD']);
  });
});


describe('mcxSessionOpen', () => {
  const at = (iso: string) => mcxSessionOpen(new Date(iso));
  it('is open Mon-Fri 09:00-23:30 IST and closed otherwise', () => {
    expect(at('2026-11-17T03:30:00Z')).toBe(true);    // Tue 09:00 IST (opens)
    expect(at('2026-11-17T03:29:00Z')).toBe(false);   // 08:59
    expect(at('2026-11-17T18:00:00Z')).toBe(false);   // 23:30 IST is the close (end exclusive)
    expect(at('2026-11-17T17:59:00Z')).toBe(true);    // 23:29 still open
    expect(at('2026-11-17T18:01:00Z')).toBe(false);   // 23:31 closed
    expect(at('2026-11-21T06:30:00Z')).toBe(false);   // Saturday midday
    expect(at('2026-11-22T06:30:00Z')).toBe(false);   // Sunday midday
  });
});
