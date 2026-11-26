// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../api', () => ({ apiRequest: vi.fn(), ApiRequestError: class extends Error {} }));
import { apiRequest } from '../api';
import { JobsPanel, ScanHistoryPanel, ScannerSchedulePanel } from './JobsScanner';
import type { JobRow, ScanRunView, ScheduleView } from './types';

const api = vi.mocked(apiRequest);
beforeEach(() => { api.mockReset(); });
afterEach(cleanup);

// TEST DATA only: shapes mirror the signals-lab API responses.
const job = (over: Partial<JobRow>): JobRow => ({ id: 'aaaaaaaa-1111', job_type: 'backtest', status: 'succeeded', attempts: 1, max_attempts: 3, progress: { pct: 100, stage: 'done' }, result: {}, error: null,
  created_at: '2026-11-12T09:00:00Z', started_at: null, finished_at: '2026-11-12T09:05:00Z', next_attempt_at: '2026-11-12T09:00:00Z', locked_by: null, payload: {}, ...over });
const SCHEDULE: ScheduleView = {
  scans_enabled: false, note: 'Scans run on the server at candle close.', strategies: [{ strategy_id: 's1', name: 'EMA Trend', market: 'equity_in', decision_timeframe: '5m', min_cadence_minutes: 5, enabled: false, cadence_minutes: 5, next_run_at: null, last_enqueued_at: null, last_skip_reason: null }],
  request_estimate: { provider_requests_per_scan_cycle: 0, basis: 'Scans read stored bars only.', dhan_documented_limits_for_reference: 'Data APIs 5 requests/second.', enabled_schedules: [] },
};

describe('JobsPanel', () => {
  it('surfaces failures first with their error, progress for running jobs, and retries a failed job', async () => {
    const jobs = [job({ id: 'f1', status: 'failed', attempts: 3, error: 'RuntimeError: provider down', finished_at: '2026-11-12T09:10:00Z' }), job({ id: 'r1', status: 'running', progress: { pct: 40, stage: 'running_backtest' } }), job({ id: 's1', status: 'succeeded' })];
    api.mockImplementation(async (path: string, init?: { method?: string }) => {
      if (init?.method === 'POST' && path === '/signals-lab/jobs/f1/retry') return job({ id: 'f1', status: 'queued', attempts: 0 });
      return jobs;
    });
    render(<JobsPanel />);
    expect((await screen.findAllByText('RuntimeError: provider down')).length).toBeGreaterThan(0);
    expect(screen.getByText('running backtest')).toBeInTheDocument();
    expect(screen.getAllByRole('progressbar').some((p) => p.getAttribute('aria-valuenow') === '40')).toBe(true);
    const groups = document.querySelectorAll('details.sl-issue-group');
    expect(groups[0].textContent).toMatch(/Failed/);
    expect(groups[0].hasAttribute('open')).toBe(true);
    fireEvent.click(within(groups[0] as HTMLElement).getByRole('button', { name: 'Retry' }));
    await waitFor(() => expect(api.mock.calls.some(([p, i]) => p === '/signals-lab/jobs/f1/retry' && (i as { method?: string }).method === 'POST')).toBe(true));
  });

  it('shows a repeated failure as one line with a count, and deletes that group only after confirming', async () => {
    const err = 'engine-backed strategies are backtest-only';
    const jobs = ['a', 'b', 'c'].map((id) => job({ id, job_type: 'scan', status: 'failed', error: err, payload: { trigger: 'schedule' } }));
    api.mockImplementation(async (path: string) => (path === '/signals-lab/jobs/delete-failed' ? { deleted: 3 } : jobs));
    render(<JobsPanel />);
    expect(await screen.findByText('3 × Scan · scheduled')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Delete these' }));
    expect(api.mock.calls.some(([p]) => p === '/signals-lab/jobs/delete-failed')).toBe(false);
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }));
    await waitFor(() => expect(api.mock.calls.some(([p]) => p === '/signals-lab/jobs/delete-failed')).toBe(true));
    const sent = api.mock.calls.find(([p]) => p === '/signals-lab/jobs/delete-failed')!;
    expect(JSON.parse((sent[1] as { body: string }).body)).toEqual({ job_type: 'scan', error: err });
  });

  it('shows a failure path and Retry when jobs cannot be loaded', async () => {
    api.mockRejectedValue(new Error('network down'));
    render(<JobsPanel />);
    expect(await screen.findByText(/Could not load jobs/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument();
  });
});

describe('ScannerSchedulePanel', () => {
  it('shows the master switch OFF by default, saves changes through PUT, and states the request volume', async () => {
    api.mockImplementation((async (path: string, init?: { method?: string; body?: string }) => {
      if (init?.method === 'PUT' && path === '/signals-lab/scanner/settings') return { ...SCHEDULE, scans_enabled: true };
      if (init?.method === 'PUT') return { ...SCHEDULE, scans_enabled: true, strategies: [{ ...SCHEDULE.strategies[0], enabled: true, next_run_at: '2026-11-13T03:50:15Z' }] };
      return SCHEDULE;
    }) as unknown as typeof apiRequest);
    render(<ScannerSchedulePanel strategies={[]} />);
    expect(await screen.findByText(/scheduled scanning is OFF/)).toBeInTheDocument();
    expect(screen.getByText(/Expected provider request volume per scan cycle: 0/)).toBeInTheDocument();
    fireEvent.click(screen.getByLabelText('Schedule EMA Trend'));
    expect(await screen.findByText(/scheduled scanning is ON/)).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText(/13 Nov 2026.*IST/)).toBeInTheDocument());
    const put = api.mock.calls.find(([p]) => p === '/signals-lab/scanner/schedule/s1')!;
    expect(JSON.parse((put[1] as { body: string }).body)).toEqual({ enabled: true, cadence_minutes: 5 });
  });
});

describe('ScanHistoryPanel', () => {
  it('lists when each scan ran, what it covered, and its signals and suppressions, linking to the rejections', async () => {
    const runs: ScanRunView[] = [{ scan_run_id: 'run-1', status: 'completed', trigger: 'schedule', started_at: '2026-11-12T09:50:15Z', finished_at: '2026-11-12T09:50:20Z', strategies: 2, pairs_scanned: 9, instruments_scanned: 5, new_signals: 1, errors: [],
      rejections: { scan_run_id: 'run-1', total: 3, by_reason: { insufficient_bars: 3 } }, jobs: [] }];
    api.mockResolvedValue(runs);
    const onShow = vi.fn();
    render(<ScanHistoryPanel onShowRejections={onShow} />);
    expect(await screen.findByText(/2 strategies · 5 instruments \(9 pairs\)/)).toBeInTheDocument();
    expect(screen.getByText('Scheduled')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /3 suppressed \(Not enough bars 3\)/ }));
    expect(onShow).toHaveBeenCalledWith('run-1');
  });
});
