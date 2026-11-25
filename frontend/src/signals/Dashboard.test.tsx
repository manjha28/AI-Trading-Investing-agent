// @vitest-environment jsdom
import { act, cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { Dashboard } from './Dashboard';
import type { Health } from './types';

afterEach(cleanup);

// Test data only: shapes mirror the backend data_health rows.
const health: Health = {
  counts: { total: 1, active: 1, suspended: 0, retired: 0 }, recent_signals: [], recent_backtests: [],
  data_health: [
    { instrument_id: 'a', symbol: 'RELIANCE', market: 'equity_in', status: 'no_data', window_days: 5, total_bars: 177901, history_start: '2024-11-04T03:45:00+00:00', history_end: '2026-10-01T09:00:00+00:00', issues: ['no 1-minute bars in the last 5 days (total stored history: 177901 bars)'] },
    { instrument_id: 'b', symbol: 'SBIN', market: 'equity_in', status: 'ok', bars: 1800, missing_pct: 0, window_days: 5, total_bars: 182347, history_start: '2024-11-04T03:45:00+00:00', history_end: '2026-11-11T07:10:00+00:00', issues: [],
      provider_unserved: { excluded_minutes: 75, note: 'x' }, provider_coverage: { provider: 'dhan', start: '15:15', end: '15:29', last_served: '15:14', minutes_per_session: 15, label: '15:15-15:29 not served by dhan', sessions_matching: 27, sessions_examined: 28 } },
  ],
};

describe('Dashboard market-data health', () => {
  it('states its window and shows recent health beside total stored history', () => {
    render(<Dashboard health={health} loading={false} error={null} goto={() => undefined} />);
    expect(screen.getByText(/last 5 days/, { selector: 'strong' })).toBeTruthy();
    expect(screen.getByText('177,901 bars')).toBeTruthy();           // RELIANCE is "no data" recently but has stored history
    expect(screen.getByText('2024-11-04 → 2026-10-01')).toBeTruthy();
  });

  it('discloses a provider-unserved window instead of counting it as missing', () => {
    render(<Dashboard health={health} loading={false} error={null} goto={() => undefined} />);
    expect(screen.getByText(/09:15-15:14 available from dhan · 15:15-15:29 not served by provider/)).toBeTruthy();
    expect(screen.getByText('excludes 75 provider-unserved min')).toBeTruthy();
  });
});

describe('Dashboard slow loading', () => {
  it('says "Still loading (slow backend)" after 10 s instead of "No data", and shows the real error if one comes back', () => {
    vi.useFakeTimers();
    try {
      const { rerender } = render(<Dashboard health={null} loading error={null} goto={() => undefined} />);
      expect(screen.getByText('Loading dashboard…')).toBeTruthy();
      act(() => { vi.advanceTimersByTime(10_500); });
      expect(screen.getByText(/Still loading \(slow backend\)/)).toBeTruthy();
      expect(screen.queryByText('No data.')).toBeNull();
      rerender(<Dashboard health={null} loading={false} error="GET /signals-lab/health failed: 504 The server took longer than 120 seconds" goto={() => undefined} />);
      expect(screen.getByText(/504 The server took longer/)).toBeTruthy();
    } finally {
      vi.useRealTimers();
    }
  });
});

describe('Dashboard between the workspace and the health answer', () => {
  it('says "Loading data health…" (not "No data.") once the workspace has loaded but health has not answered yet', () => {
    render(<Dashboard health={null} loading={false} error={null} goto={() => undefined} />);
    expect(screen.getByText('Loading data health…')).toBeTruthy();
    expect(screen.queryByText('No data.')).toBeNull();
  });
});
