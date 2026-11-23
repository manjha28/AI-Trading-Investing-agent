import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { fireEvent } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { DailyReport } from './DailyReport';

// Test data only: a hand-written API payload, never a runtime path.
const levels = { status: 'available', entry_zone: { low: '100.00', high: '103.00' }, stop: '95.00', reference_close: '101.50', prior_high_reference: '110.00', targets: [{ label: '2R', price: '115.00' }, { label: '3.5R', price: '125.00' }], derivation: { rule: 'rule text', inputs: { atr14_wilder: 2 }, tick: '0.05' } };
const conf = { score: 72.4, band: 'High', note: 'Stop is 2.0 ATR away.', parts: [{ key: 'evidence', label: 'Evidence quality', value: 80, weight: 0.4 }] };
const buy = { instrument_id: 'i1', symbol: 'AAA', name: 'Alpha Ltd', held: false, quantity: null, bucket: 'buy', action: 'Can be bought', researched_at: '2026-11-20T11:00:00Z', stale: false, last_close: '101.50',
  horizons: { swing: { recommendation: 'HOLD', score: 50, confidence: 60, why_not: [] }, positional: { recommendation: 'BUY', score: 80, confidence: 80, buy_confidence: conf, why_not: [] }, long_term: { recommendation: 'AVOID', score: 30, confidence: 60, why_not: [] } },
  levels: { positional: levels }, confidence: conf };
const hold = { instrument_id: 'i2', symbol: 'BBB', name: 'Beta Ltd', held: true, quantity: '10', bucket: 'hold', action: 'In your portfolio: keep holding.', researched_at: null, stale: false, horizons: {}, levels: {} };
const report = { as_of: '2026-11-21T05:00:00Z', buckets: { buy: [buy], hold: [hold], no_new_entry: [], not_enough_data: [] }, counts: { buy: 1, hold: 1, no_new_entry: 0, not_enough_data: 0 }, scope: 's', disclaimer: 'Decision support, not investment advice.', researching: [] };

// Nexus v2 payload (test data): a neutral factor-rank badge with a setup grade, and a "Set up, waiting" group.
const confV2 = { kind: 'factor_v2', score: 3095, band: 'High', label: 'High', grade: 'A', setup: { label: 'Grade A', checks: [{ key: 'direction', label: 'Engine direction is bullish', result: 'pass', reason: 'ok', veto: true }], failed_vetoes: [] },
  factor_rank: { rank: 12, of: 249, composite: 81.2, percentile: 95.2, text: 'Factor rank 12th of 249 (factor screen)' }, note: 'A screen, not a confidence.', parts: [{ key: 'trend_quality', label: 'Trend quality', value: 80, weight: 0.75 }] };
const waiting = { ...buy, instrument_id: 'i3', symbol: 'CCC', name: 'Gamma Ltd', bucket: 'setup_waiting', action: 'Set up, waiting', horizons: { positional: { recommendation: 'BUY', score: 80, confidence: 80, buy_confidence: { ...confV2, label: 'B', band: 'B', grade: 'B' }, why_not: [] } }, levels: { positional: { ...levels, entry_status: 'WAIT_FOR_ENTRY', entry_label: 'Wait for a pullback into 100.00-103.00' } }, confidence: { ...confV2, label: 'B', band: 'B', grade: 'B' } };
const reportV2 = { ...report, buckets: { buy: [{ ...buy, horizons: { positional: { recommendation: 'BUY', score: 80, confidence: 80, buy_confidence: confV2, why_not: [] } }, confidence: confV2 }], setup_waiting: [waiting], hold: [hold], no_new_entry: [], not_enough_data: [] }, counts: { buy: 1, setup_waiting: 1, hold: 1, no_new_entry: 0, not_enough_data: 0 } };
const mockApi = vi.fn((..._args: unknown[]): Promise<unknown> => Promise.resolve(report));
vi.mock('../api', () => ({ apiRequest: (...a: unknown[]) => mockApi(...a) }));
afterEach(() => { cleanup(); vi.clearAllMocks(); });

describe('DailyReport', () => {
  it('shows the buy card with confidence, levels and the hold row, and opens a stock', async () => {
    const onOpen = vi.fn();
    render(<DailyReport onOpen={onOpen} />);
    await waitFor(() => expect(screen.getAllByText('Alpha Ltd')[0]).toBeTruthy());
    expect(screen.getByRole('img', { name: /Confidence 72 out of 100, High/ })).toBeTruthy();
    expect(screen.getByText('₹95.00')).toBeTruthy();
    expect(screen.getByText(/keep holding/)).toBeTruthy();
    expect(screen.getByText(/Decision support, not investment advice/)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'AAA' }));
    expect(onOpen).toHaveBeenCalledWith(expect.objectContaining({ id: 'i1', symbol: 'AAA' }));
  });

  it('v2: shows a neutral factor rank with the setup grade, a Set up waiting group, and no confidence wording', async () => {
    mockApi.mockImplementation(() => Promise.resolve(reportV2));
    render(<DailyReport onOpen={vi.fn()} />);
    await waitFor(() => expect(screen.getAllByText('Alpha Ltd')[0]).toBeTruthy());
    expect(screen.getAllByText('Factor rank 12th of 249 (factor screen)').length).toBeGreaterThan(0);
    expect(screen.getByText('Gamma Ltd')).toBeTruthy();
    expect(screen.getAllByText(/Set up, waiting/).length).toBeGreaterThan(0);
    fireEvent.click(screen.getByText('Gamma Ltd').closest('button') as HTMLElement);                  // the detail panel shows the selected stock
    expect(screen.getAllByText('Wait for a pullback into 100.00-103.00').length).toBeGreaterThan(0);
    expect(screen.queryByRole('img', { name: /Confidence \d+ out of 100/ })).toBeNull();
    mockApi.mockImplementation(() => Promise.resolve(report));
  });

  it('v2: shows the event gate note and the unreliable event-calendar warning on a card', async () => {
    const gated = { ...buy, levels: { positional: { ...levels, event_gate: 'results on 2026-11-25 (2 session(s) ahead): no entry-now signal until it has passed.', gates: { event_calendar_note: 'EVENT CALENDAR UNRELIABLE: the NSE filings feed has not updated since 2026-11-05 (17 days).' } } } };
    mockApi.mockImplementation(() => Promise.resolve({ ...report, buckets: { ...report.buckets, buy: [gated] } }));
    render(<DailyReport onOpen={vi.fn()} />);
    await waitFor(() => expect(screen.getAllByText('Alpha Ltd')[0]).toBeTruthy());
    expect(screen.getByText(/results on 2026-11-25/)).toBeTruthy();
    expect(screen.getByText(/EVENT CALENDAR UNRELIABLE/)).toBeTruthy();
    mockApi.mockImplementation(() => Promise.resolve(report));
  });

  it('shows the measured win probability with its sample, band label and expected R, and nothing when the backend sends none', async () => {
    const win = { line: 'Confidence 38% (avg setup 27%) · based on 212 similar past setups', band_label: 'High (not yet separating)', tooltip: '80% interval 31-45%.', expected_r: 0.21 };
    const card = (c: typeof confV2 & { win_confidence?: typeof win }) => ({ ...buy, horizons: { positional: { recommendation: 'BUY', score: 80, confidence: 80, buy_confidence: c, why_not: [] } }, confidence: c });
    mockApi.mockImplementation(() => Promise.resolve({ ...reportV2, buckets: { ...reportV2.buckets, buy: [card({ ...confV2, win_confidence: win })], setup_waiting: [] } }));
    const first = render(<DailyReport onOpen={vi.fn()} />);
    await waitFor(() => expect(screen.getByText(/Confidence 38% \(avg setup 27%\) · based on 212 similar past setups · High \(not yet separating\) · expected \+0.21R/)).toBeTruthy());
    first.unmount();
    mockApi.mockImplementation(() => Promise.resolve({ ...reportV2, buckets: { ...reportV2.buckets, buy: [card(confV2)], setup_waiting: [] } }));
    render(<DailyReport onOpen={vi.fn()} />);
    await waitFor(() => expect(screen.getAllByText('Alpha Ltd')[0]).toBeTruthy());
    expect(screen.queryByText(/similar past setups/)).toBeNull();
    mockApi.mockImplementation(() => Promise.resolve(report));
  });

  it('v2: shows the measured line only when the backend sends one, and shows the grade alone otherwise', async () => {
    const withLine = { ...confV2, measured_line: 'T1 before stop in 34% of 119 past setups, average +0.41R after costs' };
    const card = (c: typeof confV2) => ({ ...buy, horizons: { positional: { recommendation: 'BUY', score: 80, confidence: 80, buy_confidence: c, why_not: [] } }, confidence: c });
    mockApi.mockImplementation(() => Promise.resolve({ ...reportV2, buckets: { ...reportV2.buckets, buy: [card(withLine)], setup_waiting: [] } }));
    const first = render(<DailyReport onOpen={vi.fn()} />);
    await waitFor(() => expect(screen.getByText(/T1 before stop in 34% of 119 past setups/)).toBeTruthy());
    first.unmount();
    mockApi.mockImplementation(() => Promise.resolve({ ...reportV2, buckets: { ...reportV2.buckets, buy: [card(confV2)], setup_waiting: [] } }));
    render(<DailyReport onOpen={vi.fn()} />);
    await waitFor(() => expect(screen.getAllByText('Alpha Ltd')[0]).toBeTruthy());
    expect(screen.queryByText(/past setups/)).toBeNull();
    mockApi.mockImplementation(() => Promise.resolve(report));
  });

  it('sorts Ready to buy by exactly the number shown on each card and hides the prior high when it is missing', async () => {
    const mk = (id: string, sym: string, shown: number, headline: number, withPrior: boolean) => ({ ...buy, instrument_id: id, symbol: sym, name: `${sym} Ltd`,
      horizons: { positional: { recommendation: 'BUY', score: 80, confidence: 80, buy_confidence: { ...conf, score: shown }, why_not: [] } }, confidence: { ...conf, score: headline },
      levels: { positional: { ...levels, prior_high_reference: withPrior ? '110.00' : undefined } } });
    // headline (best horizon) order would be A, B, C; the number shown on the cards is 71, 75, 72
    mockApi.mockImplementation(() => Promise.resolve({ ...report, buckets: { ...report.buckets, buy: [mk('a', 'AAA', 71, 90, true), mk('b', 'BBB', 75, 80, false), mk('c', 'CCC', 72, 70, true)] }, counts: { ...report.counts, buy: 3 } }));
    render(<DailyReport onOpen={vi.fn()} />);
    await waitFor(() => expect(screen.getAllByText('BBB Ltd')[0]).toBeTruthy());
    const order = [...document.querySelectorAll('.dr-row')].map((b) => b.querySelector('strong')?.textContent);
    expect(order).toEqual(['BBB', 'CCC', 'AAA']);                                              // the list is sorted by the number shown on each card
    expect(screen.queryByText(/prior high/)).toBeNull();                                          // BBB (selected first) has no prior high: that part of the line is hidden, not "prior high —"
    fireEvent.click(document.querySelectorAll('.dr-row')[1]);
    expect(screen.getAllByText(/prior high/).length).toBe(1);
    expect(screen.queryByText(/prior high —/)).toBeNull();
    mockApi.mockImplementation(() => Promise.resolve(report));
  });

  it('explains a horizon without data instead of printing "No data"', async () => {
    const short = { ...hold, instrument_id: 'i9', symbol: 'NEWCO', name: 'New Co Ltd', held: false, quantity: null, bucket: 'not_enough_data', action: 'Insufficient evidence: no annual statements stored.', researched_at: '2026-11-20T11:00:00Z',
      horizons: { positional: { recommendation: 'INSUFFICIENT_EVIDENCE', score: null, confidence: null, why_not: [], data_note: 'Listed recently: only 2 weekly candles stored; positional needs 40' } } };
    mockApi.mockImplementation(() => Promise.resolve({ ...report, buckets: { ...report.buckets, not_enough_data: [short] }, counts: { ...report.counts, not_enough_data: 1 } }));
    render(<DailyReport onOpen={vi.fn()} />);
    await waitFor(() => expect(screen.getByText(/Listed recently: only 2 weekly candles stored; positional needs 40/)).toBeTruthy());
    mockApi.mockImplementation(() => Promise.resolve(report));
  });

  it('lists candidates as compact rows with state, band, stop and first target, and a Held chip with the quantity', async () => {
    const held = { ...buy, held: true, quantity: '1250', last_close: '108.00', levels: { positional: { ...levels, reference_close: '108.00', entry_label: undefined } } };
    mockApi.mockImplementation(() => Promise.resolve({ ...report, buckets: { ...report.buckets, buy: [held] } }));
    render(<DailyReport onOpen={vi.fn()} />);
    const row = (await screen.findAllByText('Alpha Ltd'))[0].closest('button') as HTMLElement;
    expect(row.textContent).toContain('Held: 1,250');
    expect(row.textContent).toContain('Wait: above band');                                         // never a bare "Wait for entry"
    expect(row.textContent).toContain('₹100.00–₹103.00'); expect(row.textContent).toContain('Stop ₹95.00'); expect(row.textContent).toMatch(/T1 ₹115\.00/);
    expect(screen.getAllByText(/Wait: price ₹108\.00 is above the band/).length).toBeGreaterThan(0);   // the full reason is on the detail card
    mockApi.mockImplementation(() => Promise.resolve(report));
  });

  it('filters by held and by buy band, and the sort control says exactly what it sorts by', async () => {
    const mk = (id: string, sym: string, close: string, isHeld: boolean) => ({ ...buy, instrument_id: id, symbol: sym, name: `${sym} Ltd`, held: isHeld, quantity: isHeld ? '5' : null, last_close: close, levels: { positional: { ...levels, reference_close: close, entry_label: undefined } } });
    mockApi.mockImplementation(() => Promise.resolve({ ...report, buckets: { ...report.buckets, buy: [mk('a', 'AAA', '101.5', false), mk('b', 'BBB', '110', true)] }, counts: { ...report.counts, buy: 2 } }));
    render(<DailyReport onOpen={vi.fn()} />);
    await screen.findAllByText('AAA Ltd');
    const symbols = () => [...document.querySelectorAll('.dr-row strong')].map((n) => n.textContent);
    expect(symbols().sort()).toEqual(['AAA', 'BBB']);
    fireEvent.click(screen.getByLabelText('In buy band')); expect(symbols()).toEqual(['AAA']);
    fireEvent.click(screen.getByLabelText('In buy band')); fireEvent.click(screen.getByLabelText('Held')); expect(symbols()).toEqual(['BBB']);
    expect(screen.getByRole('option', { name: 'Confidence score shown on the card, highest first' })).toBeTruthy();
    expect(screen.getByRole('option', { name: 'Distance from price to the buy band, nearest first' })).toBeTruthy();
    mockApi.mockImplementation(() => Promise.resolve(report));
  });

  it('collapses Needs data by default and keeps the reason on each row', async () => {
    const short = { ...hold, instrument_id: 'i9', symbol: 'NEWCO', name: 'New Co Ltd', held: false, quantity: null, bucket: 'not_enough_data', action: 'Insufficient evidence: no annual statements stored.' };
    mockApi.mockImplementation(() => Promise.resolve({ ...report, buckets: { ...report.buckets, not_enough_data: [short] }, counts: { ...report.counts, not_enough_data: 1 } }));
    render(<DailyReport onOpen={vi.fn()} />);
    const summary = await screen.findByText(/Needs data \(1\)/);
    expect((summary.closest('details') as HTMLDetailsElement).open).toBe(false);
    expect(screen.getByText(/no annual statements stored/)).toBeTruthy();
    mockApi.mockImplementation(() => Promise.resolve(report));
  });
});
