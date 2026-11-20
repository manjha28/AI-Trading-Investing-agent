import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MarketWatch, type WatchWorkspace } from './MarketWatch';

const stream = vi.hoisted(() => ({ value: { status: 'active', message: null as string | null, quotes: {} as Record<string, unknown> } }));
vi.mock('../hooks/useWatchlistStream', () => ({ useWatchlistStream: () => stream.value }));
vi.mock('../components/StockAlerts', () => ({ StockAlerts: ({ instrumentId, symbol }: { instrumentId: string; symbol: string }) => <div data-testid="alerts-panel">alerts for {symbol} ({instrumentId})</div> }));
vi.mock('../components/StockEvents', () => ({ StockEvents: ({ symbol }: { symbol: string }) => <div data-testid="events-panel">actions and news for {symbol}</div> }));
vi.mock('./CommodityWatch', () => ({ CommodityWatch: () => <div>commodity section stub</div> }));
vi.mock('../components/TerminalChart', () => ({
  TerminalChart: ({ candles, ariaLabel }: { candles: unknown[]; ariaLabel?: string }) => <div data-testid="chart" aria-label={ariaLabel}>{candles.length} candles</div>,
}));

const row = (symbol: string, extra: Partial<Record<string, string | number | boolean | null>> = {}) => ({
  instrument_id: `id-${symbol}`, symbol, name: `${symbol} Ltd`, venue: 'NSE', sector: 'Energy', last_close: null, last_close_date: null,
  volume: null, previous_close: null, change: null, change_pct: null, stale: true, sessions_behind: null, reason: 'no stored daily close', ...extra,
});
const universe = [
  row('AAA', { day_low: '97.50', day_high: '103.25', last_close: '100.00', last_close_date: '2026-11-20', previous_close: '98', change: '2.00', change_pct: '2.04', volume: '5000', stale: false, sessions_behind: 0, reason: null }),
  row('BBB', { sector: 'Banks', last_close: '50.00', last_close_date: '2026-11-19', previous_close: '55', change: '-5.00', change_pct: '-9.09', volume: '900', stale: true, sessions_behind: 1, reason: 'last stored close is 2026-11-19' }),
  row('ONGC', { last_close: '234.80', last_close_date: '2026-11-11', previous_close: '233.50', change: '1.30', change_pct: '0.56', stale: true, sessions_behind: 7, reason: 'last stored close is 2026-11-11' }),
  row('CCC'),
];
const settings = { selected_provider: 'yfinance', sources: [
  { name: 'dhan', label: 'Dhan', available: true, enabled: true }, { name: 'yfinance', label: 'Yahoo Finance', available: true, enabled: true },
  { name: 'angel_one', label: 'Angel One', available: false, enabled: true },
] };

const liveQuote = (id: string, close: string, pct: string, chg: string, volume: string) => ({
  instrument_id: id, close, freshness_status: 'live', receivedAt: Date.now(), market_timestamp: '2026-11-20T10:00:00Z',
  detail: { percent_change: pct, absolute_change: chg, bar: { volume } },
});

function makeWorkspace(starred: string[] = []) {
  return {
    watchlist: starred.map((id) => ({ instrument_id: id })),
    addInstrumentToWatchlist: vi.fn(async () => undefined),
    removeInstrumentFromWatchlist: vi.fn(async () => undefined),
    setActiveView: vi.fn(),
    setOpenMarketInstrumentId: vi.fn(),
  };
}
const asWorkspace = (w: ReturnType<typeof makeWorkspace>) => w as unknown as WatchWorkspace;

function mockApi(options: { lateExtra?: boolean } = {}) {
  const calls: { url: string; method: string; body?: string }[] = [];
  let universeCalls = 0;
  let newcoAdded = false;
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    const method = (init?.method ?? 'GET').toUpperCase();
    calls.push({ url: String(url), method, body: init?.body as string | undefined });
    const path = String(url);
    if (path.endsWith('/market-data/universe')) universeCalls += 1;
    const extra = row('ABFRL', { last_close: '88.00', last_close_date: '2026-11-20', previous_close: '80', change: '8.00', change_pct: '10.00', stale: false, sessions_behind: 0, reason: null, live_feed: false });
    if (path.includes('/market-data/universe/extras/')) newcoAdded = (init?.method ?? 'GET').toUpperCase() === 'POST' ? true : false;
    const newco = row('NEWCO', { instrument_id: 'id-NEW', last_close: '12.00', last_close_date: '2026-11-20', stale: false, sessions_behind: 0, reason: null, live_feed: false, master_extra: true });
    const base = options.lateExtra && universeCalls >= 2 ? [...universe, extra] : universe;
    const withExtra = newcoAdded ? [...base, newco] : base;
    const body = path.includes('/market-data/universe') ? { count: withExtra.length, instruments: withExtra, expected_session: '2026-11-20', disclaimer: 'x' }
      : path.includes('/market-data/settings/provider') ? { ...settings, selected_provider: JSON.parse(String(init?.body)).provider }
        : path.includes('/market-data/settings') ? settings
          : path.includes('/market-data/instruments?') ? [{ id: 'id-NEW', symbol: 'NEWCO', display_name: 'New Co Ltd', venue: 'NSE', currency: 'INR' }, { id: 'id-AAA', symbol: 'AAA', display_name: 'AAA Ltd', venue: 'NSE', currency: 'INR' }]
          : path.includes('/alerts/rules') ? [{ instrument_id: 'id-AAA', enabled: true }, { instrument_id: 'id-BBB', enabled: false }, { instrument_id: null, enabled: true }]
            : path.includes('/history') ? { bars: [1, 2, 3].map((n) => ({ timestamp: `2026-11-20T0${n}:00:00Z`, open: '1', high: '2', low: '1', close: '2', volume: '1' })) }
              : {};
    return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } });
  }));
  return calls;
}

// waits for the symbol's row in the TABLE (the suggestions list can also show the same symbol)
const tableRow = async (symbol: string) => waitFor(() => {
  const found = Array.from(document.querySelectorAll('.watch-table tbody tr')).find((tr) => tr.querySelector('strong')?.textContent === symbol);
  if (!found) throw new Error(`no table row for ${symbol}`);
  return found as HTMLElement;
});
const symbolsShown = () => within(screen.getByRole('table')).getAllByRole('row').slice(1).map((r) => r.querySelector('strong')?.textContent);

beforeEach(() => { stream.value = { status: 'active', message: null, quotes: {} }; });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe('MarketWatch · equities', () => {
  it('lists every symbol, shows stored values until ticks arrive, and never invents a missing one', async () => {
    mockApi();
    render(<MarketWatch workspace={asWorkspace(makeWorkspace())} />);
    await screen.findByText('AAA');
    expect(symbolsShown()).toEqual(['AAA', 'BBB', 'CCC', 'ONGC']);
    expect(screen.getByText('+2.04%')).toBeTruthy();
    const ccc = screen.getByText('CCC').closest('tr')!;
    expect(within(ccc).getAllByText('—').length).toBeGreaterThanOrEqual(4); // unknown stays unknown, not zero
    expect(ccc.getAttribute('title')).toBe('no stored daily close');
  });

  it('never presents an old close as current: the ONGC case is labelled stale with its date, a one-session gap is "prior close"', async () => {
    mockApi();
    render(<MarketWatch workspace={asWorkspace(makeWorkspace())} />);
    await screen.findByText('ONGC');
    const ongc = screen.getByText('ONGC').closest('tr')!;
    expect(within(ongc).getByText('2026-11-11')).toBeTruthy();
    expect(within(ongc).getByText('stale')).toBeTruthy();
    expect(ongc.getAttribute('title')).toContain('last stored close is 2026-11-11');
    expect(within(screen.getByText('BBB').closest('tr')!).getByText('prior close')).toBeTruthy();
    expect(within(screen.getByText('AAA').closest('tr')!).queryByText('stale')).toBeNull();
    expect(screen.getByText(/2 need a refreshed close/)).toBeTruthy(); // CCC (none) + ONGC (7 sessions); BBB is fine overnight
    expect(screen.getByText('1 stale')).toBeTruthy();
    expect(screen.getByText('2026-11-20', { selector: '.watch-strip b' })).toBeTruthy(); // the latest completed session
  });

  it('updates a row from the live feed (price, change, volume) and counts live symbols', async () => {
    stream.value = { status: 'active', message: null, quotes: { 'id-ONGC': liveQuote('id-ONGC', '223.46', '-1.46', '-3.30', '777000') } };
    mockApi();
    render(<MarketWatch workspace={asWorkspace(makeWorkspace())} />);
    await screen.findByText('ONGC');
    const ongc = screen.getByText('ONGC').closest('tr')!;
    expect(within(ongc).getByText('223.46')).toBeTruthy();   // the live price replaces the stale stored one
    expect(within(ongc).getByText('-1.46%')).toBeTruthy();
    expect(within(ongc).queryByText('stale')).toBeNull();     // a live row is not shown as stale
    expect(screen.getByText('● LIVE')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Live' }));
    expect(symbolsShown()).toEqual(['ONGC']);
  });

  it('filters by search, sector and gainers/losers, and sorts with unknown values last', async () => {
    mockApi();
    render(<MarketWatch workspace={asWorkspace(makeWorkspace())} />);
    await screen.findByText('AAA');
    fireEvent.change(screen.getByLabelText('Search symbols'), { target: { value: 'bb' } });
    expect(symbolsShown()).toEqual(['BBB']);
    fireEvent.change(screen.getByLabelText('Search symbols'), { target: { value: '' } });
    fireEvent.change(screen.getByLabelText('Sector'), { target: { value: 'Banks' } });
    expect(symbolsShown()).toEqual(['BBB']);
    fireEvent.change(screen.getByLabelText('Sector'), { target: { value: '' } });
    fireEvent.click(screen.getByRole('button', { name: 'Gainers' }));
    expect(symbolsShown()).toEqual(['AAA', 'ONGC']);
    fireEvent.click(screen.getByRole('button', { name: 'Losers' }));
    expect(symbolsShown()).toEqual(['BBB']);
    fireEvent.click(screen.getByRole('button', { name: 'All' }));
    fireEvent.click(screen.getByRole('button', { name: /Chg %/ })); // descending: unknown (CCC) always last
    expect(symbolsShown()).toEqual(['AAA', 'ONGC', 'BBB', 'CCC']);
    fireEvent.click(screen.getByRole('button', { name: /Chg %/ }));
    expect(symbolsShown()).toEqual(['BBB', 'ONGC', 'AAA', 'CCC']);
  });

  it('offers the closest symbols for a typo instead of an empty table, and they can be starred', async () => {
    mockApi();
    const workspace = makeWorkspace();
    render(<MarketWatch workspace={asWorkspace(workspace)} />);
    await screen.findByText('AAA');
    fireEvent.change(screen.getByLabelText('Search symbols'), { target: { value: 'ONGX' } });   // typo of ONGC
    expect(symbolsShown()).toEqual(['ONGC']);
    expect(screen.getByText(/No exact match for “ONGX”/)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Add ONGC to watchlist' }));
    expect(workspace.addInstrumentToWatchlist).toHaveBeenCalledWith('id-ONGC', 'ONGC');
    fireEvent.change(screen.getByLabelText('Search symbols'), { target: { value: 'ONGC' } });   // exact: no notice
    expect(screen.queryByText(/No exact match/)).toBeNull();
    fireEvent.change(screen.getByLabelText('Search symbols'), { target: { value: 'ZZZZZZ' } }); // nothing close: honest empty
    expect(symbolsShown()).toEqual([]);
    expect(screen.getByText('No symbols match these filters.')).toBeTruthy();
  });

  it('adds and removes symbols in the Overview watchlist with the star, and filters to the watchlist', async () => {
    mockApi();
    const workspace = makeWorkspace(['id-BBB']);
    render(<MarketWatch workspace={asWorkspace(workspace)} />);
    await screen.findByText('AAA');
    fireEvent.click(screen.getByRole('button', { name: 'Add AAA to watchlist' }));
    expect(workspace.addInstrumentToWatchlist).toHaveBeenCalledWith('id-AAA', 'AAA');
    fireEvent.click(screen.getByRole('button', { name: 'Remove BBB from watchlist' }));
    expect(workspace.removeInstrumentFromWatchlist).toHaveBeenCalledWith('id-BBB');
    expect(screen.getByRole('button', { name: 'Remove BBB from watchlist' }).getAttribute('aria-pressed')).toBe('true');
    fireEvent.click(screen.getByRole('button', { name: 'Watchlist' }));
    expect(symbolsShown()).toEqual(['BBB']);
  });

  it('uses one search box; an instrument outside the table gets a dedicated Add to market watch button, then can be starred', async () => {
    const calls = mockApi();
    const workspace = makeWorkspace(['id-AAA']);
    render(<MarketWatch workspace={asWorkspace(workspace)} />);
    await screen.findByText('AAA');
    expect(screen.getAllByRole('searchbox')).toHaveLength(1);   // no second "find" box
    fireEvent.change(screen.getByLabelText('Search symbols'), { target: { value: 'new' } });
    const results = await screen.findByRole('list', { name: 'Not in the market table' });
    expect(within(results).getByText('NEWCO')).toBeTruthy();
    expect(within(results).queryByText('AAA')).toBeNull();   // AAA is in the table already, so it is not offered again
    fireEvent.click(within(results).getByRole('button', { name: '＋ Add to market watch' }));
    // the master list is updated first (not the watchlist) ...
    await waitFor(() => expect(calls.some((c) => c.method === 'POST' && c.url.endsWith('/market-data/universe/extras/id-NEW'))).toBe(true));
    expect(workspace.addInstrumentToWatchlist).not.toHaveBeenCalled();
    // ... then the new row is in the table and can be starred
    const row = await tableRow('NEWCO');
    expect(row.querySelector('.watch-star')).toBeTruthy();
    fireEvent.click(within(row).getByRole('button', { name: 'Add NEWCO to watchlist' }));
    expect(workspace.addInstrumentToWatchlist).toHaveBeenCalledWith('id-NEW', 'NEWCO');
    expect(calls.some((c) => c.url.includes('/market-data/instruments?') && c.url.includes('query=new'))).toBe(true);
  });

  it('removes an instrument that was added to the market watch', async () => {
    const calls = mockApi();
    render(<MarketWatch workspace={asWorkspace(makeWorkspace())} />);
    await screen.findByText('AAA');
    fireEvent.change(screen.getByLabelText('Search symbols'), { target: { value: 'new' } });
    fireEvent.click(await screen.findByRole('button', { name: '＋ Add to market watch' }));
    const row = await tableRow('NEWCO');
    fireEvent.click(row);
    fireEvent.click(await screen.findByRole('button', { name: 'Remove from market watch' }));
    await waitFor(() => expect(calls.some((c) => c.method === 'DELETE' && c.url.endsWith('/market-data/universe/extras/id-NEW'))).toBe(true));
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Remove from market watch' })).toBeNull());
  });

  it('shows each symbol\'s day range: stored low/high until the live quote supplies today\'s', async () => {
    stream.value = { status: 'active', message: null, quotes: { 'id-ONGC': { ...liveQuote('id-ONGC', '223.46', '-1.46', '-3.30', '777000'), detail: { percent_change: '-1.46', absolute_change: '-3.30', bar: { volume: '777000', low: '221.10', high: '226.80' } } } } };
    mockApi();
    render(<MarketWatch workspace={asWorkspace(makeWorkspace())} />);
    await screen.findByText('AAA');
    const fmt = (n: number) => n.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    const aaa = within(screen.getByText('AAA').closest('tr')!.querySelector('.watch-range') as HTMLElement);
    expect(aaa.getByText(fmt(97.5))).toBeTruthy();       // stored day low
    expect(aaa.getByText(fmt(103.25))).toBeTruthy();     // stored day high
    const ongc = within(screen.getByText('ONGC').closest('tr')!.querySelector('.watch-range') as HTMLElement);
    expect(ongc.getByText(fmt(221.1))).toBeTruthy();     // live low replaces the (stale) stored value
    expect(ongc.getByText(fmt(226.8))).toBeTruthy();
    const ccc = screen.getByText('CCC').closest('tr')!.querySelector('.watch-range') as HTMLElement;
    expect(ccc.textContent).toBe('——');                   // unknown range stays unknown, never 0.00
  });

  it('does not look anything up for a one-letter search, and offers nothing when every match is already listed', async () => {
    const calls = mockApi();
    render(<MarketWatch workspace={asWorkspace(makeWorkspace())} />);
    await screen.findByText('AAA');
    fireEvent.change(screen.getByLabelText('Search symbols'), { target: { value: 'a' } });
    await new Promise((resolve) => setTimeout(resolve, 450));
    expect(calls.some((c) => c.url.includes('/market-data/instruments?'))).toBe(false);
    expect(screen.queryByRole('list', { name: 'Not in the market table' })).toBeNull();
  });

  it('runs the header search term through the same box', async () => {
    const calls = mockApi();
    render(<MarketWatch workspace={asWorkspace(makeWorkspace())} initialQuery="ongc" searchNonce={1} />);
    await screen.findByText('ONGC');
    expect(symbolsShown()).toEqual(['ONGC']);
    expect((screen.getByLabelText('Search symbols') as HTMLInputElement).value).toBe('ongc');
    await waitFor(() => expect(calls.some((c) => c.url.includes('/market-data/instruments?') && c.url.includes('query=ongc'))).toBe(true));
  });

  it('charts the selected symbol within the API limit, offers history, and drills into full details', async () => {
    const calls = mockApi();
    const workspace = makeWorkspace();
    render(<MarketWatch workspace={asWorkspace(workspace)} />);
    fireEvent.click((await screen.findByText('AAA')).closest('tr')!);
    expect((await screen.findByTestId('chart')).textContent).toBe('3 candles');
    const history = calls.find((c) => c.url.includes('/instruments/id-AAA/history?') && c.method === 'GET')!;
    expect(new URL(history.url, 'http://x').searchParams.get('limit')).toBe('500');
    fireEvent.click(screen.getByRole('button', { name: 'Load history' }));
    await waitFor(() => expect(calls.some((c) => c.method === 'POST' && c.url.includes('/instruments/id-AAA/history/sync'))).toBe(true));
    fireEvent.click(screen.getByRole('button', { name: 'Full details ›' }));
    expect(workspace.setOpenMarketInstrumentId).toHaveBeenCalledWith('id-AAA');
    expect(workspace.setActiveView).toHaveBeenCalledWith('market-data');
  });

  it('refreshes closes for symbols with none or a genuinely stale one (not for a one-session gap), then reloads', async () => {
    const calls = mockApi();
    render(<MarketWatch workspace={asWorkspace(makeWorkspace())} />);
    await screen.findByText('CCC');
    fireEvent.click(screen.getByRole('button', { name: 'Load missing closes (2)' }));
    await waitFor(() => expect(calls.filter((c) => c.method === 'POST').map((c) => c.url.match(/instruments\/([^/]+)\//)?.[1]).sort()).toEqual(['id-CCC', 'id-ONGC']));
    await waitFor(() => expect(calls.filter((c) => c.url.endsWith('/market-data/universe')).length).toBeGreaterThanOrEqual(2));
  });

  it('says why when the live feed is unavailable', async () => {
    stream.value = { status: 'unavailable', message: 'Live feed access was refused.', quotes: {} };
    mockApi();
    render(<MarketWatch workspace={asWorkspace(makeWorkspace())} />);
    expect(await screen.findByText('Live feed access was refused.')).toBeTruthy();
    expect(screen.getByText(/FEED UNAVAILABLE/)).toBeTruthy();
  });
});

describe('MarketWatch · watchlist additions', () => {
  it('shows a symbol starred after the table loaded, without a page reload, and says it has no live feed', async () => {
    const calls = mockApi({ lateExtra: true });
    const workspace = makeWorkspace([]);
    const { rerender } = render(<MarketWatch workspace={asWorkspace(workspace)} />);
    await screen.findByText('AAA');
    expect(screen.queryByText('ABFRL')).toBeNull();
    // the user adds ABFRL from the search suggestions / Overview: the shared watchlist now contains it
    rerender(<MarketWatch workspace={asWorkspace(makeWorkspace(['id-ABFRL']))} />);
    const abfrl = (await screen.findByText('ABFRL')).closest('tr')!;
    expect(calls.filter((c) => c.url.endsWith('/market-data/universe')).length).toBe(2);
    expect(within(abfrl).getByText('no live feed')).toBeTruthy();
    expect(within(abfrl).getByRole('button', { name: 'Remove ABFRL from watchlist' }).getAttribute('aria-pressed')).toBe('true');
    fireEvent.click(screen.getByRole('button', { name: 'Watchlist' }));
    expect(symbolsShown()).toEqual(['ABFRL']);
  });

  it('does not reload in a loop when a starred id never becomes listable', async () => {
    const calls = mockApi();
    render(<MarketWatch workspace={asWorkspace(makeWorkspace(['id-GHOST']))} />);
    await screen.findByText('AAA');
    await new Promise((resolve) => setTimeout(resolve, 300));
    expect(calls.filter((c) => c.url.endsWith('/market-data/universe')).length).toBe(2); // initial + one retry, then stop
  });
});

describe('MarketWatch · shell', () => {
  it('offers the data source (only usable ones) and saves the choice', async () => {
    const calls = mockApi();
    render(<MarketWatch workspace={asWorkspace(makeWorkspace())} />);
    const select = await screen.findByLabelText('Data source');
    expect(within(select).getAllByRole('option').map((o) => o.textContent)).toEqual(['Dhan', 'Yahoo Finance']); // Angel One is not available
    fireEvent.change(select, { target: { value: 'dhan' } });
    await waitFor(() => expect(calls.some((c) => c.method === 'PUT' && c.url.endsWith('/market-data/settings/provider') && JSON.parse(c.body ?? '{}').provider === 'dhan')).toBe(true));
  });

  it('switches between equities and the MCX commodity section', async () => {
    mockApi();
    render(<MarketWatch workspace={asWorkspace(makeWorkspace())} />);
    await screen.findByText('AAA');
    fireEvent.click(screen.getByRole('tab', { name: 'Commodities · MCX' }));
    expect(await screen.findByText('commodity section stub')).toBeTruthy();
    fireEvent.click(screen.getByRole('tab', { name: 'Equities · NSE' }));
    expect(await screen.findByText('AAA')).toBeTruthy();
  });
});

describe('MarketWatch · alerts, corporate actions and news per stock', () => {
  it('marks the rows that have an active alert, and not paused ones', async () => {
    mockApi();
    render(<MarketWatch workspace={asWorkspace(makeWorkspace())} />);
    await screen.findByText('AAA');
    await waitFor(() => expect(within(screen.getByText('AAA').closest('tr')!).getByLabelText('has alert')).toBeTruthy());
    expect(within(screen.getByText('BBB').closest('tr')!).queryByLabelText('has alert')).toBeNull();       // that rule is paused
    expect(within(screen.getByText('CCC').closest('tr')!).queryByLabelText('has alert')).toBeNull();
  });

  it('gives a selected stock Chart, Alerts and Corporate actions & news tabs, with quick buttons beside Full details', async () => {
    mockApi();
    render(<MarketWatch workspace={asWorkspace(makeWorkspace())} />);
    fireEvent.click((await screen.findByText('AAA')).closest('tr')!);
    const tabs = within(await screen.findByRole('tablist', { name: 'Stock details' }));
    expect(tabs.getAllByRole('tab').map((t) => t.textContent?.replace(' ●', '').trim())).toEqual(['Chart', 'Alerts', 'Corporate actions & news']);
    expect(screen.queryByTestId('alerts-panel')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Alert' }));
    expect((await screen.findByTestId('alerts-panel')).textContent).toBe('alerts for AAA (id-AAA)');
    expect(screen.queryByTestId('chart')).toBeNull();                                                        // the chart gives way to the panel
    fireEvent.click(tabs.getByRole('tab', { name: 'Corporate actions & news' }));
    expect((await screen.findByTestId('events-panel')).textContent).toBe('actions and news for AAA');
    expect(screen.queryByTestId('alerts-panel')).toBeNull();
    fireEvent.click(tabs.getByRole('tab', { name: 'Chart' }));
    expect(await screen.findByTestId('chart')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Actions & news' }));
    expect(await screen.findByTestId('events-panel')).toBeTruthy();
  });

  it('opens the stock an alert is about on its Alerts tab, even when a search was hiding it', async () => {
    mockApi();
    const workspace = asWorkspace(makeWorkspace());
    const { rerender } = render(<MarketWatch workspace={workspace} initialQuery="aaa" searchNonce={1} />);
    await screen.findByText('AAA');
    expect(symbolsShown()).toEqual(['AAA']);
    rerender(<MarketWatch workspace={workspace} initialQuery="aaa" searchNonce={1} focus={{ instrumentId: 'id-ONGC', tab: 'alerts', nonce: 1 }} />);
    expect((await screen.findByTestId('alerts-panel')).textContent).toBe('alerts for ONGC (id-ONGC)');
    expect(symbolsShown()).toContain('ONGC');                                                                // the filter was cleared so the row is visible
    expect((screen.getByLabelText('Search symbols') as HTMLInputElement).value).toBe('');
    rerender(<MarketWatch workspace={workspace} focus={{ instrumentId: 'id-BBB', tab: 'events', nonce: 2 }} />);
    expect((await screen.findByTestId('events-panel')).textContent).toBe('actions and news for BBB');
  });
});
