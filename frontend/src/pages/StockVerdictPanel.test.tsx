import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { StockVerdictPanel } from './StockVerdictPanel';

const mockApi = vi.hoisted(() => vi.fn());
vi.mock('../api', () => ({ apiRequest: mockApi }));
afterEach(() => { cleanup(); mockApi.mockReset(); });

const lens = (score: number | null, direction: string, facts: string[]) => ({ score, direction, facts, coverage: 1 });
const hz = (verdict: string, extra: Record<string, unknown> = {}) => ({ verdict, evidence_score: 58.4, evidence: { label: 'Evidence score 58 (not yet validated)' }, confidence: { line: null, band: null, validated: false }, reasons: ['r1', 'r2', 'r3', 'r4'], veto: null, size: 1,
  levels: { entry_zone: { low: '100.01', high: '103.02' }, stop: '95.00', targets: [{ label: '2R', price: '115.03', risk_reward: 2 }] }, grade: 'A', lenses: { chart: lens(72, 'positive', ['Close above a rising stack']), fundamentals: lens(80, 'positive', ['Class: quality compounder.']), news_events: lens(null, 'neutral', ['No scheduled event']) }, ...extra });
const payload = { enabled: true, symbol: 'AAA', as_of: '2026-11-22T10:00:00Z', headline: { horizon: 'positional', verdict: 'buy_on_trigger', evidence: 58.4 }, horizons: { swing: hz('avoid', { veto: 'event', levels: null, size: 0 }), positional: hz('buy_on_trigger'), long_term: hz('not_now', { size: 0.5 }) },
  multibagger: { class: 'quality_compounder', checklist: '12 of 17 checks passed', timing: 'wait_for_chart', failed_or_unknown: ['pledge: unknown'] }, regime: { regime: 'range', facts: ['NIFTY above a rising 200-day average.'] }, risks_and_events: ['results on 2026-11-26'], disclaimer: 'Decision support, not investment advice.' };

describe('StockVerdictPanel', () => {
  it('shows the headline, three horizon rows with entry, stop, T1, size, three reasons and the veto, lens cards, the multibagger class and the evidence score as not yet validated', async () => {
    mockApi.mockResolvedValue(payload);
    render(<StockVerdictPanel symbol="AAA" />);
    await waitFor(() => expect(screen.getByLabelText('Stock verdict')).toBeTruthy());
    expect(screen.getByText(/Buy on trigger · Positional/)).toBeTruthy();
    expect(screen.getByText(/veto: event/)).toBeTruthy();
    expect(screen.getAllByText('Evidence score 58 (not yet validated)').length).toBe(3);
    expect(screen.queryByText(/High|Medium|Low/)).toBeNull();                                  // no band before calibration
    expect(screen.getAllByText('r3').length).toBe(3);
    expect(screen.queryByText('r4')).toBeNull();                                                // only the top three reasons
    expect(screen.getAllByText('100.00 – 103.00').length).toBeGreaterThan(0);                  // tick-rounded
    expect(screen.getByText('50%')).toBeTruthy();                                              // half-size class
    expect(screen.getByText('Close above a rising stack')).toBeTruthy();
    expect(screen.getByText(/quality compounder · 12 of 17 checks passed/)).toBeTruthy();
    expect(screen.getByText('results on 2026-11-26')).toBeTruthy();
  });
  it('shows the shadow note while the flag is off, and says so when no verdict exists yet', async () => {
    mockApi.mockResolvedValue({ ...payload, shadow: true });
    const { rerender } = render(<StockVerdictPanel symbol="AAA" />);
    await waitFor(() => expect(screen.getByText(/Shadow mode: nightly results/)).toBeTruthy());
    mockApi.mockResolvedValue({ enabled: true, as_of: null, message: 'No nightly verdict has been computed for this stock yet.', horizons: {}, disclaimer: 'x' });
    rerender(<StockVerdictPanel symbol="BBB" />);
    await waitFor(() => expect(screen.getByText(/No nightly verdict/)).toBeTruthy());
  });
});
