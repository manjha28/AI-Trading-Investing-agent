import { render, screen, waitFor } from '@testing-library/react';
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

vi.mock('../api', () => ({ apiRequest: vi.fn(() => Promise.resolve(report)) }));
afterEach(() => vi.clearAllMocks());

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
});
