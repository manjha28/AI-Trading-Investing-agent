// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('../api', () => ({ apiRequest: vi.fn(), ApiRequestError: class extends Error {} }));
import { apiRequest } from '../api';
import { BackfillPanel } from './BackfillPanel';
import type { BackfillJob, HealthRow } from './types';

const api = vi.mocked(apiRequest);
afterEach(() => { cleanup(); api.mockReset(); });

const rows: HealthRow[] = [{ instrument_id: 'sbin', symbol: 'SBIN', market: 'equity_in', status: 'ok', issues: [] }];
// Test data only: the shape the backend job record returns.
const job = (over: Partial<BackfillJob>): BackfillJob => ({
  id: 'j1', status: 'running', error: null, window: { start: '2026-10-12T00:00:00Z', end: '2026-11-11T00:00:00Z' }, refuse_threshold_pct: 5, pause: null,
  current: { instrument_id: 'sbin', symbol: 'SBIN' }, instruments_total: 1, instruments_done: 0,
  instruments: [{ instrument_id: 'sbin', symbol: 'SBIN', status: 'running', before: { status: 'refuse', bars: 7407, expected_minutes: 7875, missing_minutes: 468, missing_pct: 5.943 }, after: null, bars_fetched: 0, attempts: 1, reason: null, unrecoverable: [], provider_coverage: null }],
  ...over,
});

describe('BackfillPanel', () => {
  it('starts a backfill for the selected instruments and shows a rate-limit pause as progress, not a freeze', async () => {
    const paused = job({ pause: { until: new Date(Date.now() + 45_000).toISOString(), seconds: 45, attempt: 2, of: 3, symbol: 'SBIN', reason: 'x' } });
    api.mockResolvedValueOnce(paused);
    render(<BackfillPanel rows={rows} onFinished={() => undefined} />);
    fireEvent.click(screen.getByLabelText('SBIN'));
    fireEvent.click(screen.getByRole('button', { name: /Backfill 1 selected/ }));
    await waitFor(() => expect(screen.getByText(/Dhan is rate-limiting history requests/)).toBeTruthy());
    expect(screen.getByText(/retry 2 of 3/)).toBeTruthy();
    const [path, init] = api.mock.calls[0] as [string, { method: string; body: string }];
    expect(path).toBe('/market-data/backfill/intraday-1m');
    expect(JSON.parse(init.body).instrument_ids).toEqual(['sbin']);
  });

  it('shows before and after missing share, the excluded provider window, and the recorded reason for unrecoverable ranges', async () => {
    const done = job({
      status: 'completed', instruments_done: 1, current: null,
      instruments: [{ instrument_id: 'sbin', symbol: 'SBIN', status: 'unrecoverable_gaps', bars_fetched: 120, attempts: 1, reason: null,
        before: { status: 'refuse', bars: 7407, expected_minutes: 7875, missing_minutes: 468, missing_pct: 5.943 },
        after: { status: 'warn', bars: 7527, expected_minutes: 7560, missing_minutes: 154, missing_pct: 2.037 },
        unrecoverable: [{ day: '2026-11-11', missing_minutes: 154, reason: 'provider_returned_no_candles_after_successful_fetch' }],
        provider_coverage: { provider: 'dhan', start: '15:15', end: '15:29', last_served: '15:14', minutes_per_session: 15, label: '15:15-15:29 not served by dhan', sessions_matching: 27, sessions_examined: 28 } }],
    });
    api.mockResolvedValueOnce(done);
    render(<BackfillPanel rows={rows} onFinished={() => undefined} />);
    fireEvent.click(screen.getByLabelText('SBIN'));
    fireEvent.click(screen.getByRole('button', { name: /Backfill 1 selected/ }));
    await waitFor(() => expect(screen.getByText('5.943%')).toBeTruthy());
    expect(screen.getByText('2.037%')).toBeTruthy();
    expect(screen.getByText(/15:15-15:29 not served by dhan; excluded from the 5% refusal share/)).toBeTruthy();
    expect(screen.getByText(/provider_returned_no_candles_after_successful_fetch/)).toBeTruthy();
  });
});
