import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { AlertsBell } from './AlertsBell';
import { StockAlerts } from './StockAlerts';
import { StockEvents } from './StockEvents';

const calls: { path: string; method: string; body?: Record<string, unknown> }[] = [];
let rules: Record<string, unknown>[];
let events: Record<string, unknown>[];
let actions: Record<string, unknown>[];
let news: Record<string, unknown>[];
let failRules = false;

vi.mock('../api', () => ({
  ApiRequestError: class extends Error {},
  apiRequest: async (path: string, init: { method?: string; body?: string } = {}) => {
    const method = init.method ?? 'GET';
    calls.push({ path, method, body: init.body ? JSON.parse(init.body) : undefined });
    if (path === '/alerts/rules' && method === 'GET') { if (failRules) throw new Error('Rules unavailable'); return rules; }
    if (path === '/alerts/rules' && method === 'POST') return {};
    if (path.startsWith('/alerts/rules/')) return {};
    if (path.startsWith('/alerts/events?status=new')) return events.filter((e) => e.status === 'new');
    if (path.startsWith('/alerts/events?')) return events;
    if (path.startsWith('/alerts/events/')) { const id = path.split('/').pop(); events = events.map((e) => (e.id === id ? { ...e, status: 'read' } : e)); return {}; }
    if (path === '/alerts/evaluate') return { fired: [{}] };
    if (path.startsWith('/corporate-actions')) return actions;
    if (path.startsWith('/news')) return news;
    if (path.startsWith('/market-data/instruments/')) return { symbol: path.endsWith('/i1') ? 'SBIN' : 'TCS' };
    throw new Error(`unexpected ${method} ${path}`);
  },
}));

const rule = (id: string, instrument: string, type = 'price_above', extra: Record<string, unknown> = {}) => ({ id, scope: 'instrument', instrument_id: instrument, rule_type: type, params: { price: '900' }, severity: 'attention', enabled: true, last_status: null, last_reason: null, ...extra });
const ev = (id: string, instrument: string | null, status = 'new', type = 'price_above') => ({ id, rule_id: 'r', instrument_id: instrument, fired_at: '2026-11-23T05:00:00Z', evidence: { symbol: instrument === 'i1' ? 'SBIN' : 'TCS', price: '905.10' }, status, severity: 'attention', rule_type: type });

beforeEach(() => {
  calls.length = 0; failRules = false;
  rules = [rule('r1', 'i1'), rule('r2', 'i2', 'price_below', { params: { price: '3000' } })];
  events = [ev('e1', 'i1'), ev('e2', 'i2'), ev('e3', 'i1', 'read')];
  actions = [
    { id: 'a1', action_type: 'dividend', ex_date: '2020-06-01', announcement_date: null, record_date: null, ratio: null, amount: '8.00', source: 'yfinance', verified_at: null, is_discrepant: false, discrepancy_note: null },
    { id: 'a2', action_type: 'split', ex_date: '2999-01-01', announcement_date: null, record_date: null, ratio: '2:1', amount: null, source: 'nse', verified_at: '2026-01-01T00:00:00Z', is_discrepant: false, discrepancy_note: null },
    { id: 'a3', action_type: 'bonus', ex_date: '2021-03-01', announcement_date: null, record_date: null, ratio: '1:1', amount: null, source: 'yfinance', verified_at: null, is_discrepant: true, discrepancy_note: 'ratio differs between sources' },
  ];
  news = [
    { external_id: 'n1', source: 'nse', source_url: 'https://nse.example/ann/1', instrument_id: 'i1', published_at: '2026-11-20T04:00:00Z', publisher: 'NSE', category: 'Board meeting', headline: 'Board meeting on 5 Dec to consider results' },
    { external_id: 'n2', source: 'pti', source_url: null, instrument_id: null, published_at: '2026-11-20T05:00:00Z', publisher: 'PTI', category: null, headline: 'Market-wide headline that is not about this stock' },
  ];
});
afterEach(() => { cleanup(); vi.useRealTimers(); });

describe('StockAlerts', () => {
  it('shows only this stock\'s alerts and what has fired for it, never another stock\'s', async () => {
    render(<StockAlerts instrumentId="i1" symbol="SBIN" lastPrice="900.00" />);
    const panel = within(await screen.findByLabelText('Alerts for SBIN'));
    await panel.findByRole('button', { name: 'Pause' });
    expect(panel.queryByText('Price falls below', { selector: 'strong' })).toBeNull();        // the TCS rule is not listed
    const items = panel.getAllByRole('listitem');
    expect(items.length).toBe(3);                                                           // 1 rule + the 2 events for i1 (TCS's event is excluded)
    expect(items.map((i) => i.textContent).join(' ')).not.toContain('3000');
  });

  it('prefills a sensible price, sends exactly the rule the user chose and refreshes', async () => {
    render(<StockAlerts instrumentId="i1" symbol="SBIN" lastPrice="900.00" />);
    await screen.findByRole('button', { name: 'Pause' });
    expect((screen.getByLabelText('Price (₹)') as HTMLInputElement).value).toBe('927.00');          // 3% above the last price
    fireEvent.change(screen.getByLabelText('Price (₹)'), { target: { value: '950' } });
    fireEvent.click(screen.getByRole('button', { name: '＋ Add alert' }));
    expect(await screen.findByText(/Alert added for SBIN/)).toBeTruthy();
    const post = calls.find((c) => c.path === '/alerts/rules' && c.method === 'POST')!;
    expect(post.body).toEqual({ scope: 'instrument', instrument_id: 'i1', rule_type: 'price_above', params: { price: '950' }, severity: 'attention', cooldown_seconds: 3600, enabled: true });
    expect(calls.filter((c) => c.path === '/alerts/rules' && c.method === 'GET').length).toBeGreaterThanOrEqual(2);
  });

  it('offers RSI, % move and MA-cross alerts with integer periods sent as numbers', async () => {
    render(<StockAlerts instrumentId="i1" symbol="SBIN" lastPrice={null} />);
    await screen.findByRole('button', { name: 'Pause' });
    fireEvent.change(screen.getByLabelText('Alert type'), { target: { value: 'rsi_threshold' } });
    fireEvent.change(screen.getByLabelText('Level'), { target: { value: '25' } });
    fireEvent.change(screen.getByLabelText('Direction'), { target: { value: 'below' } });
    fireEvent.click(screen.getByRole('button', { name: '＋ Add alert' }));
    await waitFor(() => expect(calls.find((c) => c.method === 'POST')?.body).toMatchObject({ rule_type: 'rsi_threshold', params: { period: 14, threshold: '25', direction: 'below' } }));
    expect(['price_above', 'price_below', 'pct_move_nd', 'rsi_threshold', 'ma_cross']).toContain('ma_cross');
    fireEvent.change(screen.getByLabelText('Alert type'), { target: { value: 'ma_cross' } });
    expect((screen.getByLabelText('Fast') as HTMLInputElement).value).toBe('9');
  });

  it('pauses, resumes and deletes an alert, and can check for triggers now', async () => {
    render(<StockAlerts instrumentId="i1" symbol="SBIN" lastPrice="900" />);
    await screen.findByRole('button', { name: 'Pause' });
    fireEvent.click(screen.getByRole('button', { name: 'Pause' }));
    await waitFor(() => expect(calls.some((c) => c.path === '/alerts/rules/r1' && c.method === 'PATCH' && (c.body as { enabled: boolean }).enabled === false)).toBe(true));
    fireEvent.click(screen.getByRole('button', { name: 'Delete Price rises above alert' }));
    await waitFor(() => expect(calls.some((c) => c.path === '/alerts/rules/r1' && c.method === 'DELETE')).toBe(true));
    fireEvent.click(screen.getByRole('button', { name: 'Check now' }));
    expect(await screen.findByText('1 alert(s) fired.')).toBeTruthy();
  });

  it('shows a load failure instead of an empty "none yet"', async () => {
    failRules = true;
    render(<StockAlerts instrumentId="i1" symbol="SBIN" lastPrice="900" />);
    expect(await screen.findByRole('alert')).toHaveProperty('textContent', 'Rules unavailable');
  });
});

describe('StockEvents', () => {
  it('lists upcoming actions first, with ex-date, ratio or amount, source status and a sources-disagree warning', async () => {
    render(<StockEvents instrumentId="i1" symbol="SBIN" />);
    const panel = within(await screen.findByLabelText('Corporate actions and news for SBIN'));
    const items = (await panel.findAllByText(/ex-date/)).map((n) => n.closest('li')!.textContent);
    expect(items[0]).toContain('Stock split');                                          // the future ex-date comes first
    expect(items[0]).toContain('ratio 2:1');
    expect(panel.getByText(/upcoming/)).toBeTruthy();
    expect(items.some((t) => t?.includes('Dividend') && t.includes('₹8.00 per share'))).toBe(true);
    expect(panel.getByText(/sources disagree/).getAttribute('title')).toBe('ratio differs between sources');
    expect(panel.getByText('verified')).toBeTruthy();
    expect(calls.some((c) => c.path === '/corporate-actions?instrument_id=i1')).toBe(true);
  });

  it('shows this stock\'s announcements with safe links and drops market-wide items', async () => {
    render(<StockEvents instrumentId="i1" symbol="SBIN" />);
    const link = await screen.findByRole('link', { name: 'Board meeting on 5 Dec to consider results' });
    expect(link.getAttribute('href')).toBe('https://nse.example/ann/1');
    expect(link.getAttribute('rel')).toContain('noopener');
    expect(screen.queryByText(/Market-wide headline/)).toBeNull();
  });

  it('says plainly when nothing is recorded', async () => {
    actions = []; news = [];
    render(<StockEvents instrumentId="i1" symbol="SBIN" />);
    expect(await screen.findByText(/No split, bonus or dividend is recorded for SBIN/)).toBeTruthy();
    expect(await screen.findByText(/No announcement or news is stored for SBIN yet/)).toBeTruthy();
  });
});

describe('AlertsBell', () => {
  it('shows how many alerts are new and lists them with the stock name and the rule', async () => {
    render(<AlertsBell onOpenStock={vi.fn()} />);
    const bell = await screen.findByRole('button', { name: 'Alerts: 2 new' });
    expect(within(bell).getByText('2')).toBeTruthy();
    fireEvent.click(bell);
    const dialog = within(await screen.findByRole('dialog', { name: 'New alerts' }));
    expect(await dialog.findByText('SBIN')).toBeTruthy();                                // names are looked up, not shown as ids
    expect(dialog.getByText('TCS')).toBeTruthy();
    expect(dialog.getAllByText(/Price rises above/).length).toBe(2);
    expect(dialog.getAllByText(/price 905.10/).length).toBe(2);
  });

  it('opens the stock on click, marks that alert read, and the badge drops', async () => {
    const onOpen = vi.fn();
    render(<AlertsBell onOpenStock={onOpen} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Alerts: 2 new' }));
    fireEvent.click(await within(await screen.findByRole('dialog')).findByRole('button', { name: /SBIN/ }));
    expect(onOpen).toHaveBeenCalledWith('i1');
    await waitFor(() => expect(calls.some((c) => c.path === '/alerts/events/e1' && c.method === 'PATCH' && (c.body as { status: string }).status === 'read')).toBe(true));
    expect(await screen.findByRole('button', { name: 'Alerts: 1 new' })).toBeTruthy();
  });

  it('marks everything read and shows an empty state with where to add alerts', async () => {
    render(<AlertsBell onOpenStock={vi.fn()} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Alerts: 2 new' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Mark all read' }));
    expect(await screen.findByText(/Nothing new\. Add an alert from any stock in Market watch/)).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Alerts: none new' })).toBeTruthy();
  });

  it('closes with Escape and on an outside click', async () => {
    render(<div><span>outside</span><AlertsBell onOpenStock={vi.fn()} /></div>);
    fireEvent.click(await screen.findByRole('button', { name: /Alerts/ }));
    expect(screen.getByRole('dialog')).toBeTruthy();
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(screen.queryByRole('dialog')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: /Alerts/ }));
    fireEvent.mouseDown(screen.getByText('outside'));
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('keeps itself current: it polls, and reloads at once when told an alert changed', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
    const { rerender } = render(<AlertsBell onOpenStock={vi.fn()} refreshKey={0} />);
    await screen.findByRole('button', { name: 'Alerts: 2 new' });
    const loads = () => calls.filter((c) => c.path.startsWith('/alerts/events?status=new')).length;
    const base = loads();
    await act(async () => { await vi.advanceTimersByTimeAsync(61_000); });
    expect(loads()).toBeGreaterThan(base);
    const polled = loads();
    rerender(<AlertsBell onOpenStock={vi.fn()} refreshKey={1} />);
    await waitFor(() => expect(loads()).toBeGreaterThan(polled));
  });
});
