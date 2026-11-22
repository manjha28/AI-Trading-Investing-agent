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
    await waitFor(() => expect(screen.getByText('Alpha Ltd')).toBeTruthy());
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
    await waitFor(() => expect(screen.getByText('Alpha Ltd')).toBeTruthy());
    expect(screen.getAllByText('Factor rank 12th of 249 (factor screen)').length).toBeGreaterThan(0);
    expect(screen.getByText('Gamma Ltd')).toBeTruthy();
    expect(screen.getAllByText(/Set up, waiting/).length).toBeGreaterThan(0);
    expect(screen.getAllByText('Wait for a pullback into 100.00-103.00').length).toBeGreaterThan(0);
    expect(screen.queryByRole('img', { name: /Confidence \d+ out of 100/ })).toBeNull();
    mockApi.mockImplementation(() => Promise.resolve(report));
  });

  it('v2: shows the event gate note and the unreliable event-calendar warning on a card', async () => {
    const gated = { ...buy, levels: { positional: { ...levels, event_gate: 'results on 2026-11-25 (2 session(s) ahead): no entry-now signal until it has passed.', gates: { event_calendar_note: 'EVENT CALENDAR UNRELIABLE: the NSE filings feed has not updated since 2026-11-05 (17 days).' } } } };
    mockApi.mockImplementation(() => Promise.resolve({ ...report, buckets: { ...report.buckets, buy: [gated] } }));
    render(<DailyReport onOpen={vi.fn()} />);
    await waitFor(() => expect(screen.getByText('Alpha Ltd')).toBeTruthy());
    expect(screen.getByText(/results on 2026-11-25/)).toBeTruthy();
    expect(screen.getByText(/EVENT CALENDAR UNRELIABLE/)).toBeTruthy();
    mockApi.mockImplementation(() => Promise.resolve(report));
  });
});
