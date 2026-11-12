// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../api', () => ({ apiRequest: vi.fn(), ApiRequestError: class extends Error {} }));
import { apiRequest } from '../api';
import { LifecycleChip } from './LifecycleChip';
import { LiveSignals } from './LiveSignals';
import { STATE_META, STATE_ORDER } from './lifecycle';
import type { InstrumentRow, SignalRow, StrategyRow } from './types';

const api = vi.mocked(apiRequest);
beforeEach(() => { api.mockReset(); });
afterEach(cleanup);

// TEST DATA only: shapes mirror the signals-lab API responses.
const STRATEGIES = [{ id: 's1', name: 'EMA Trend', kind: 'intraday', status: 'active' }] as unknown as StrategyRow[];
const INSTRUMENTS = [{ id: 'i1', symbol: 'SBIN' }, { id: 'i2', symbol: 'RELIANCE' }] as unknown as InstrumentRow[];
const signal = (over: Partial<SignalRow>): SignalRow => ({
  id: 'sig1', strategy_id: 's1', strategy_name: 'EMA Trend', strategy_version: 1, instrument_id: 'i1', symbol: 'SBIN', market: 'equity_in', direction: 'long', status: 'ready',
  suppression_reason: null, timeframe: '5m', signal_time: '2026-11-12T09:50:00Z', reference_price: '800.10', currency: 'INR', indicators: {}, conditions_met: [], conditions_unmet: [],
  rationale: 'test', data_freshness: { state: 'fresh', age_minutes: 2 }, expires_at: '2026-11-12T10:50:00Z', created_at: '2026-11-12T09:51:00Z', ...over,
} as SignalRow);
const REJ = [
  { id: 'r1', scan_run_id: 'run-aaaa1111', strategy_id: 's1', strategy_version: 3, instrument_id: 'i1', direction: null, reason_code: 'insufficient_bars', reason_detail: 'only 12 bars', recorded_at: '2026-11-12T09:51:00Z', observed_data_time: null, payload: {} },
  { id: 'r2', scan_run_id: 'run-aaaa1111', strategy_id: 's1', strategy_version: 3, instrument_id: 'i2', direction: 'long', reason_code: 'stale_spot', reason_detail: 'spot 40 min old', recorded_at: '2026-11-12T09:51:00Z', observed_data_time: '2026-11-12T09:10:00Z', payload: {} },
];
const QUEUED = { scan_run_id: 'run-aaaa1111', job_ids: ['j1'], strategies_queued: 1, skipped_in_flight: [] };
const SCAN_VIEW = { scan_run_id: 'run-aaaa1111', status: 'completed', trigger: 'manual', started_at: '2026-11-12T09:51:00Z', strategies: 1, pairs_scanned: 9, instruments_scanned: 9, new_signals: 0, errors: [], rejections: { scan_run_id: 'run-aaaa1111', total: 7, by_reason: { insufficient_bars: 6, stale_spot: 1 } } };
const SCAN = { 'POST /signals-lab/signals/scan': QUEUED, 'GET /signals-lab/scan-runs/run-aaaa1111': SCAN_VIEW };

function route(over: Record<string, unknown> = {}) {
  api.mockImplementation(async (path: string, init?: { method?: string }) => {
    const key = `${init?.method ?? 'GET'} ${path.split('?')[0]}`;
    if (key in over) { const v = over[key]; return typeof v === 'function' ? (v as () => unknown)() : v; }
    if (key === 'GET /signals-lab/signals') return [];
    if (key === 'GET /signals-lab/signals/rejections/summary') return { scan_run_id: null, total: 0, by_reason: {} };
    if (key === 'GET /signals-lab/signals/rejections') return REJ;
    throw new Error(`unrouted ${key}`);
  });
}
const renderLive = () => render(<LiveSignals strategies={STRATEGIES} instruments={INSTRUMENTS} onChanged={() => undefined} />);

describe('scan result summary', () => {
  it('an empty scan states strategies, pairs scanned and suppression counts by reason, never a bare "No signals"', async () => {
    route(SCAN);
    renderLive();
    fireEvent.click(await screen.findByRole('button', { name: 'Scan now' }));
    await waitFor(() => expect(screen.getAllByText(/7 suppressed \(Not enough bars 6, Stale underlying spot 1\)/).length).toBeGreaterThan(0));
    const summary = document.querySelector('.sl-scan-summary')!;
    expect(summary.textContent).toMatch(/1 active strategy · 9 instruments \(9 strategy\/instrument pairs\) scanned · 0 new signals · 7 suppressed/);
    expect(screen.queryByText(/^No signals$/)).toBeNull();
    expect(screen.getByText(/The last scan found 0 new signal\(s\) and 7 suppressed/)).toBeInTheDocument();
    expect(screen.queryByLabelText(/Auto-scan/)).toBeNull();                       // the browser timer is gone; scanning is scheduled on the server
  });

  it('the suppressed count opens the rejections view for that scan run', async () => {
    route(SCAN);
    renderLive();
    fireEvent.click(await screen.findByRole('button', { name: 'Scan now' }));
    fireEvent.click(await screen.findByRole('button', { name: /7 suppressed/ }));
    expect(await screen.findByRole('region', { name: 'Suppressed candidates' })).toBeInTheDocument();
    expect(api.mock.calls.some(([p]) => String(p).includes('scan_run_id=run-aaaa1111'))).toBe(true);
  });
});

describe('rejections view', () => {
  it('filters by reason_code, shows readable labels with the raw code, and names instruments from the lists', async () => {
    route();
    renderLive();
    fireEvent.click(await screen.findByRole('button', { name: /Show suppressed candidates/ }));
    const panel = await screen.findByRole('region', { name: 'Suppressed candidates' });
    await waitFor(() => expect(within(panel).getByText('only 12 bars')).toBeInTheDocument());
    expect(within(panel).getByText('insufficient_bars')).toBeInTheDocument();          // raw code still accessible
    expect(within(panel).getAllByText('Not enough bars').length).toBeGreaterThan(0);   // readable label
    fireEvent.change(within(panel).getByLabelText('Filter by reason'), { target: { value: 'stale_spot' } });
    expect(within(panel).queryByText('only 12 bars')).toBeNull();
    expect(within(panel).getByText('spot 40 min old')).toBeInTheDocument();
    expect(within(panel).getByText('RELIANCE')).toBeInTheDocument();
    expect(within(panel).getByText('v3')).toBeInTheDocument();                    // strategy version now comes from the API
  });

  it('shows a visible failure path with retry instead of an endless spinner', async () => {
    route({ 'GET /signals-lab/signals/rejections': () => { throw new Error('boom'); } });
    renderLive();
    fireEvent.click(await screen.findByRole('button', { name: /Show suppressed candidates/ }));
    expect(await screen.findByText(/Could not load rejections/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument();
  });
});

describe('lifecycle chips', () => {
  it('renders all nine states, colours by stage not outcome, and gives TARGET and STOPPED the same group', () => {
    const { container } = render(<>{STATE_ORDER.map((s) => <LifecycleChip key={s} status={s} />)}</>);
    expect(container.querySelectorAll('[data-state]')).toHaveLength(9);
    expect(STATE_META.target.group).toBe(STATE_META.stopped.group);
    expect(container.querySelector('[data-state="target"]')!.className).toContain('sl-state-resolved');
    expect(container.querySelector('[data-state="stopped"]')!.className).toContain('sl-state-resolved');
    for (const el of container.querySelectorAll('[data-state]')) expect(el.className).not.toMatch(/sl-pill-(ok|bad)/);   // no green/red tone classes
    expect(screen.getByText(/PARTIALLY REALISED/)).toBeInTheDocument();
  });
});

describe('timeline and cancel', () => {
  const events = [
    { event_id: 'e1', from_state: null, to_state: 'ready', event_time: '2026-11-12T09:51:00Z', observed_data_time: null, reason_code: 'backfill_genesis', reason_detail: null, strategy_version_id: 'v', source: 'migration_backfill', payload: {}, backfilled: true },
    { event_id: 'e2', from_state: 'ready', to_state: 'active', event_time: '2026-11-12T09:55:00Z', observed_data_time: '2026-11-12T09:50:00Z', reason_code: 'confirmed_next_scan', reason_detail: null, strategy_version_id: 'v', source: 'scan', payload: {}, backfilled: false },
  ];

  it('labels recorded vs market-data time, shows — and a backfilled marker for a null observed_data_time, never substituting event_time', async () => {
    route({ 'GET /signals-lab/signals': [signal({})], 'GET /signals-lab/signals/sig1/events': events });
    renderLive();
    fireEvent.click(await screen.findByText('800.10 INR'));
    const timeline = await screen.findByRole('region', { name: 'Signal timeline' });
    await waitFor(() => expect(timeline.querySelectorAll('li')).toHaveLength(2));
    const labels = [...timeline.querySelectorAll('dt')].map((x) => x.textContent);
    expect(labels).toEqual(['Recorded', 'Market data as-of', 'Recorded', 'Market data as-of']);
    expect(within(timeline).getByText('backfilled')).toBeInTheDocument();
    const firstEvent = timeline.querySelectorAll('li')[0];
    const dds = firstEvent.querySelectorAll('dd');
    expect(dds[0].textContent).toMatch(/IST$/);
    expect(dds[1].textContent).toBe('—');                                              // not event_time
    expect(timeline.querySelectorAll('li')[1].querySelectorAll('dd')[1].textContent).toMatch(/3:20.*IST|03:20.*IST/i);
  });

  it('hides the cancel action on terminal states', async () => {
    for (const state of ['target', 'stopped', 'invalidated', 'expired', 'cancelled']) {
      cleanup();
      route({ 'GET /signals-lab/signals': [signal({ status: state })], 'GET /signals-lab/signals/sig1/events': events });
      renderLive();
      fireEvent.click(await screen.findByText('800.10 INR'));
      await screen.findByRole('region', { name: 'Signal timeline' });
      expect(screen.queryByRole('button', { name: 'Cancel signal' })).toBeNull();
      expect(screen.getByText(/final state/)).toBeInTheDocument();
    }
  });

  it('cancels a non-terminal signal only after confirmation and updates the timeline and chip in place', async () => {
    let cancelled = false;
    const afterCancel = [...events, { event_id: 'e3', from_state: 'active', to_state: 'cancelled', event_time: '2026-11-12T10:00:00Z', observed_data_time: '2026-11-12T09:50:00Z', reason_code: 'owner_cancel', reason_detail: null, strategy_version_id: 'v', source: 'manual', payload: {}, backfilled: false }];
    route({
      'GET /signals-lab/signals': () => [signal({ status: cancelled ? 'cancelled' : 'active' })],
      'GET /signals-lab/signals/sig1/events': () => (cancelled ? afterCancel : events),
      'POST /signals-lab/signals/sig1/cancel': () => { cancelled = true; return signal({ status: 'cancelled' }); },
    });
    renderLive();
    fireEvent.click(await screen.findByText('800.10 INR'));
    fireEvent.click(await screen.findByRole('button', { name: 'Cancel signal' }));
    expect(api.mock.calls.some(([p]) => String(p).endsWith('/cancel'))).toBe(false);          // nothing sent before confirming
    const dialog = screen.getByRole('dialog');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel signal' }));
    await waitFor(() => expect(screen.getByText(/final state/)).toBeInTheDocument());
    const timeline = screen.getByRole('region', { name: 'Signal timeline' });
    await waitFor(() => expect(within(timeline).getByText('Cancelled by owner')).toBeInTheDocument());
    expect(screen.queryByRole('button', { name: 'Cancel signal' })).toBeNull();
  });
});
