import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { Multibagger, clearMultibaggerCache } from './Multibagger';

const mockApi = vi.hoisted(() => vi.fn());
vi.mock('../api', () => ({ apiRequest: mockApi }));
beforeEach(() => { clearMultibaggerCache(); window.sessionStorage.clear(); });
afterEach(() => { cleanup(); mockApi.mockReset(); });

// TEST DATA: 271 rows shaped like the real nightly result (class mix from the 23 Nov run), generated here.
const MIX: Record<string, number> = { avoid: 119, watch: 47, not_a_fit: 46, not_tradable: 30, not_assessable: 28, emerging: 1 };
function make(): { items: unknown[]; counts: Record<string, number> } {
  const items: unknown[] = []; let n = 0;
  for (const [klass, count] of Object.entries(MIX)) for (let k = 0; k < count; k += 1, n += 1) {
    items.push({ symbol: `S${String(n).padStart(3, '0')}`, instrument_id: `id${n}`, class: klass, rank: null, scores: { Q: n % 9 === 0 ? null : 30 + (n % 60), G: n % 4 === 0 ? null : 20 + (n % 70), V: null, T: n % 5 === 0 ? 0 : null, X: null },
      checklist: `${n % 14} of 18 checks passed`, checks_passed: n % 14, checks_total: 18, unknown_count: 3, unknown_ids: ['surveillance', 'pledge', 'auditor_or_regulatory'], fail_count: 2, timing: klass === 'watch' && n % 2 ? 'buy_zone' : 'wait_for_chart',
      kind: 'default', reasons: [], class_if_unknown_checks_cleared: klass === 'watch' ? 'quality_compounder' : null });
  }
  return { items, counts: MIX };
}
const response = () => ({ enabled: true, shadow: true, as_of: '2026-11-23', model_version: 'multibagger-0.1.0-provisional', disclaimer: 'Decision support, not investment advice.', ...make() });
const rowsIn = () => document.querySelectorAll('tr[data-row]');

describe('Multibagger screen', () => {
  it('opens on a compact table: excluded classes hidden, at most 60 rows in the DOM, and the shadow note once', async () => {
    mockApi.mockResolvedValue(response());
    render(<Multibagger />);
    await screen.findByRole('table', { name: 'Multibagger screen' });
    expect(rowsIn().length).toBeGreaterThan(0); expect(rowsIn().length).toBeLessThanOrEqual(60);
    expect(screen.getByRole('table').getAttribute('aria-rowcount')).toBe(String(47 + 46 + 1));            // watch + not_a_fit + emerging; avoid / not tradable / not assessable are behind the toggle
    expect(screen.getAllByText(/Shadow mode/).length).toBe(1);
    expect(screen.queryByRole('columnheader', { name: 'Rank' })).toBeNull();                              // no rank anywhere: no Rank column
    expect(mockApi).toHaveBeenCalledTimes(1);
  });

  it('derives the summary and the shared data gaps from the data instead of repeating them per row', async () => {
    mockApi.mockResolvedValue(response());
    render(<Multibagger />);
    const summary = await screen.findByText(/candidates ·/);
    expect(summary.textContent).toContain('0 candidates · 0 compounders · 1 emerging · 47 watch.');
    expect(summary.textContent).toContain('ASM/GSM, pledge and auditor data are not stored');
    expect(summary.textContent).toContain('keep every stock below candidate');
    expect(screen.queryAllByText(/pledge of promoter holding/).length).toBe(0);
  });

  it('shows excluded classes on request, filters by class and by search, and sorts when a header is clicked', async () => {
    mockApi.mockResolvedValue(response());
    render(<Multibagger />);
    await screen.findByRole('table', { name: 'Multibagger screen' });
    fireEvent.click(screen.getByLabelText(/Show excluded/));
    expect(screen.getByRole('table').getAttribute('aria-rowcount')).toBe('271');
    fireEvent.click(screen.getByRole('tab', { name: /Not a fit/ }));
    expect(screen.getByRole('table').getAttribute('aria-rowcount')).toBe('46');
    fireEvent.click(screen.getByRole('tab', { name: /All/ }));
    fireEvent.change(screen.getByLabelText('Search symbol'), { target: { value: 's005' } });
    expect(screen.getByRole('table').getAttribute('aria-rowcount')).toBe('1');
    fireEvent.change(screen.getByLabelText('Search symbol'), { target: { value: '' } });
    const header = screen.getByRole('columnheader', { name: /^Q/ });
    fireEvent.click(within(header).getByRole('button'));
    expect(header.getAttribute('aria-sort')).toBe('descending');
    const first = rowsIn()[0].textContent ?? '';
    expect(first).toMatch(/8\d|89/);                                                                       // highest Q first; stocks with no Q score sort last
  });

  it('shows unknown scores as n/a with a reason, never as an empty bar', async () => {
    mockApi.mockResolvedValue(response());
    render(<Multibagger />);
    await screen.findByRole('table', { name: 'Multibagger screen' });
    expect(screen.getAllByLabelText(/not available. No score: fewer than 70% of its inputs are stored/).length).toBeGreaterThan(0);
  });

  it('opens a side sheet with the full checklist grouped, a Verify list, and Open research', async () => {
    const open = vi.fn();
    mockApi.mockImplementation((path: string) => Promise.resolve(path.includes('/research/multibagger/') ? {
      symbol: 'S119', class: 'watch', as_of: '2026-11-23', scores: { Q: 72, G: null, V: 55, T: 0, X: null }, reasons: ['capped: pledge'], checklist: '13 of 18 checks passed', timing: 'wait_for_chart', kind: 'default',
      class_if_unknown_checks_cleared: { class: 'quality_compounder' },
      checks: [{ id: 'liquidity', stage: 'gate', value: 50, reason: 'Rs 50 crore vs the floor', result: 'pass' }, { id: 'pledge', stage: 'veto', value: null, reason: 'not stored', result: 'unknown' }, { id: 'leverage_veto', stage: 'veto', value: 3, reason: 'D/E 3 (max 1)', result: 'fail' }],
    } : response()));
    render(<Multibagger onOpen={open} />);
    await screen.findByRole('table', { name: 'Multibagger screen' });
    fireEvent.click(rowsIn()[0]);
    const sheet = await screen.findByRole('dialog');
    await within(sheet).findByText(/Failed \(1\)/);
    expect(within(sheet).getByText(/Unknown \(1\)/)).toBeTruthy(); expect(within(sheet).getByText(/Passed \(1\)/)).toBeTruthy();
    expect(within(sheet).getByText(/it could reach Compounders/)).toBeTruthy();
    fireEvent.click(within(sheet).getByRole('button', { name: 'Open research' }));
    expect(open).toHaveBeenCalled();
    fireEvent.keyDown(document, { key: 'Escape' });
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  });

  it('dismisses the shadow banner for the session but keeps the Shadow chip', async () => {
    mockApi.mockResolvedValue(response());
    render(<Multibagger />);
    await screen.findByText(/Shadow mode/);
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }));
    expect(screen.queryByText(/Shadow mode/)).toBeNull();
    expect(screen.getByText('Shadow')).toBeTruthy();
  });

  it('shows the real error with Retry instead of an empty table', async () => {
    mockApi.mockRejectedValueOnce(new Error('HTTP 502 from the backend')).mockResolvedValue(response());
    render(<Multibagger />);
    expect((await screen.findByRole('alert')).textContent).toContain('HTTP 502 from the backend');
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    await screen.findByRole('table', { name: 'Multibagger screen' });
  });
});
