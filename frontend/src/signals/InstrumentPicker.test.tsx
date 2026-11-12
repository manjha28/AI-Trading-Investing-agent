// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import { useState } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../api', () => ({ apiRequest: vi.fn(), ApiRequestError: class extends Error {} }));
import { apiRequest } from '../api';
import { InstrumentPicker } from './InstrumentPicker';
import { StyledSelect } from './StyledSelect';
import type { BackfillJob, InstrumentRow } from './types';

const api = vi.mocked(apiRequest);
beforeEach(() => { api.mockReset(); });
afterEach(() => { cleanup(); vi.useRealTimers(); });

// TEST DATA only.
const STORED = [
  { id: 'sbin', symbol: 'SBIN', name: 'State Bank of India', market: 'equity_in', currency: 'INR', bars_1m: 182347, first_bar: null, last_bar: null },
  { id: 'jpl', symbol: 'JPL', name: 'Jpl Ltd', market: 'equity_in', currency: 'INR', bars_1m: 3740, first_bar: null, last_bar: null },
] as unknown as InstrumentRow[];
const REMOTE = [{ id: 'tata', symbol: 'TATAMOTORS', display_name: 'Tata Motors', venue: 'NSE' }, { id: 'sbin', symbol: 'SBIN', display_name: 'State Bank of India', venue: 'NSE' }];
const job = (over: Partial<BackfillJob>): BackfillJob => ({
  id: 'j1', status: 'running', error: null, window: { start: 'a', end: 'b' }, refuse_threshold_pct: 5, pause: null, current: null, instruments_total: 1, instruments_done: 0,
  instruments: [{ instrument_id: 'tata', symbol: 'TATAMOTORS', status: 'running', before: { status: 'refuse', bars: 0, expected_minutes: 7875, missing_minutes: 7875, missing_pct: 100 }, after: null, bars_fetched: 0, attempts: 1, reason: null, unrecoverable: [], provider_coverage: null }],
  ...over,
});

function Harness({ onStored = () => undefined, stored = STORED }: { onStored?: () => void; stored?: InstrumentRow[] }) {
  const [chosen, setChosen] = useState<string[]>([]);
  return <><InstrumentPicker stored={stored} market="equity_in" chosen={chosen} onChange={setChosen} window={{ start: '2026-10-12', end: '2026-11-11' }} onStoredChanged={onStored} /><output aria-label="chosen">{chosen.join(',')}</output></>;
}
const open = () => fireEvent.focus(screen.getByLabelText('Search instruments'));

describe('InstrumentPicker search', () => {
  it('filters as you type over stored instruments AND provider-known ones, showing each row\'s data state inline', async () => {
    api.mockResolvedValue(REMOTE);
    render(<Harness />);
    open();
    expect(screen.getByRole('option', { name: /SBIN.*182,347 bars · ready/ })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: /JPL.*3,740 bars · thin history/ })).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Search instruments'), { target: { value: 'tata' } });
    expect(screen.queryByRole('option', { name: /SBIN/ })).toBeNull();                     // local filter is immediate
    const row = await screen.findByRole('option', { name: /TATAMOTORS/ });                  // known to the provider, no bars stored
    expect(row).toHaveTextContent('no 1m data');
    expect(within(row).getByRole('button', { name: 'Fetch TATAMOTORS' })).toBeInTheDocument();
    expect(api.mock.calls[0][0]).toBe('/market-data/instruments?query=tata&limit=20');       // the existing global-search endpoint
  });

  it('supports multi-select with removable chips and keyboard navigation', async () => {
    api.mockResolvedValue([]);
    render(<Harness />);
    const input = screen.getByLabelText('Search instruments');
    open();
    fireEvent.mouseDown(screen.getByRole('option', { name: /SBIN/ }));
    fireEvent.mouseDown(screen.getByRole('option', { name: /JPL/ }));
    expect(screen.getByLabelText('chosen')).toHaveTextContent('sbin,jpl');                  // two instruments without reopening anything
    expect(screen.getByRole('button', { name: 'Remove SBIN' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Remove SBIN' }));
    expect(screen.getByLabelText('chosen')).toHaveTextContent(/^jpl$/);
    fireEvent.keyDown(input, { key: 'ArrowDown' });
    fireEvent.keyDown(input, { key: 'Enter' });                                             // second row (JPL) was active after one ArrowDown from the first
    expect(screen.getByLabelText('chosen').textContent).toBe('');
    fireEvent.keyDown(input, { key: 'ArrowUp' });
    fireEvent.keyDown(input, { key: 'Enter' });
    expect(screen.getByLabelText('chosen')).toHaveTextContent('sbin');
    fireEvent.keyDown(input, { key: 'Backspace' });                                         // empty query: Backspace removes the last chip
    expect(screen.getByLabelText('chosen').textContent).toBe('');
  });
});

describe('InstrumentPicker fetch on demand', () => {
  it('goes none -> fetching -> rate-limit countdown -> completed, then reloads the stored list; unrecoverable ranges show the recorded reason', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const onStored = vi.fn();
    let polls = 0;
    api.mockImplementation((async (path: string, init?: { method?: string }) => {
      if (path.startsWith('/market-data/instruments?')) return [REMOTE[0]];
      if (init?.method === 'POST') return job({});
      polls += 1;
      if (polls === 1) return job({ pause: { until: new Date(Date.now() + 45_000).toISOString(), seconds: 45, attempt: 2, of: 3, symbol: 'TATAMOTORS', reason: 'x' }, instruments: [{ ...job({}).instruments[0], status: 'waiting_rate_limit' }] });
      return job({ status: 'completed', instruments_done: 1, instruments: [{ ...job({}).instruments[0], status: 'unrecoverable_gaps', bars_fetched: 5000,
        after: { status: 'warn', bars: 5000, expected_minutes: 7875, missing_minutes: 200, missing_pct: 2.5 }, unrecoverable: [{ day: '2026-11-11', missing_minutes: 200, reason: 'provider_returned_no_candles_after_successful_fetch' }] }] });
    }) as unknown as typeof apiRequest);
    render(<Harness onStored={onStored} />);
    open();
    fireEvent.change(screen.getByLabelText('Search instruments'), { target: { value: 'tata' } });
    fireEvent.mouseDown(await screen.findByRole('button', { name: 'Fetch TATAMOTORS' }));
    const post = api.mock.calls.find(([, i]) => (i as { method?: string })?.method === 'POST')!;
    expect(post[0]).toBe('/market-data/backfill/intraday-1m');                              // the existing endpoint; nothing new
    expect(JSON.parse((post[1] as { body: string }).body)).toEqual({ instrument_ids: ['tata'], start: '2026-10-12', end: '2026-11-11' });
    expect(await screen.findByText(/Fetching…/)).toBeInTheDocument();
    await act(async () => { await vi.advanceTimersByTimeAsync(2100); });
    expect(await screen.findByText(/Dhan rate limit · waiting/)).toBeInTheDocument();       // a countdown, not a frozen row
    expect(screen.getByText(/retry 2 of 3/)).toBeInTheDocument();
    await act(async () => { await vi.advanceTimersByTimeAsync(2100); });
    await waitFor(() => expect(onStored).toHaveBeenCalledTimes(1));                          // parent reloads stored bars: the row becomes selectable with its count
    expect(await screen.findByText(/provider has no data for some minutes: provider_returned_no_candles_after_successful_fetch/)).toBeInTheDocument();
    expect(screen.getByText(/100% → 2.5% missing/)).toBeInTheDocument();
  });

  it('refuses to start a second backfill while another is running and says why', async () => {
    api.mockImplementation((async (path: string, init?: { method?: string }) => {
      if (path.startsWith('/market-data/instruments?')) return [REMOTE[0]];
      if (init?.method === 'POST') return { ...job({}), already_running: true, instruments: [{ ...job({}).instruments[0], instrument_id: 'other', symbol: 'OTHER' }] };
      return job({});
    }) as unknown as typeof apiRequest);
    render(<Harness />);
    open();
    fireEvent.change(screen.getByLabelText('Search instruments'), { target: { value: 'tata' } });
    fireEvent.mouseDown(await screen.findByRole('button', { name: 'Fetch TATAMOTORS' }));
    expect(await screen.findByRole('alert')).toHaveTextContent(/Another 1-minute backfill is already running/);
  });
});

describe('StyledSelect', () => {
  it('opens a themed listbox, selects with the keyboard and mouse, and is not a native <select>', () => {
    const onChange = vi.fn();
    const { container } = render(<StyledSelect ariaLabel="Direction" value="both" onChange={onChange} options={[{ value: 'both', label: 'Long + short' }, { value: 'long', label: 'Long only' }]} />);
    expect(container.querySelector('select')).toBeNull();
    const btn = screen.getByRole('combobox', { name: 'Direction' });
    fireEvent.keyDown(btn, { key: 'ArrowDown' });
    expect(screen.getByRole('listbox', { name: 'Direction' })).toBeInTheDocument();
    fireEvent.keyDown(btn, { key: 'ArrowDown' });
    fireEvent.keyDown(btn, { key: 'Enter' });
    expect(onChange).toHaveBeenCalledWith('long');
    fireEvent.click(btn);
    fireEvent.mouseDown(screen.getByRole('option', { name: 'Long + short' }));
    expect(onChange).toHaveBeenLastCalledWith('both');
    fireEvent.click(btn);
    fireEvent.keyDown(btn, { key: 'Escape' });
    expect(screen.queryByRole('listbox')).toBeNull();
  });
});
