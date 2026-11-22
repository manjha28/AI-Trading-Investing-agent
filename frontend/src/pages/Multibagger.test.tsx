import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { Multibagger } from './Multibagger';

const mockApi = vi.hoisted(() => vi.fn());
vi.mock('../api', () => ({ apiRequest: mockApi }));
afterEach(() => { cleanup(); mockApi.mockReset(); });

describe('Multibagger screen', () => {
  it('shows the results in shadow mode with a note that nothing uses them yet', async () => {
    mockApi.mockResolvedValue({ enabled: true, shadow: true, as_of: '2026-11-22', model_version: 'm', counts: { watch: 1 }, disclaimer: 'Decision support, not investment advice.',
      items: [{ symbol: 'ABC', instrument_id: 'i1', class: 'watch', rank: null, scores: { Q: 72, G: null, V: null, T: null, X: null }, checklist: '3 of 9', timing: null, kind: 'default', reasons: [], failed_or_unknown: [], class_if_unknown_checks_cleared: null }] });
    render(<Multibagger />);
    await waitFor(() => expect(screen.getByText('ABC')).toBeTruthy());
    expect(screen.getByText(/Shadow mode: these results are computed every night/)).toBeTruthy();
    expect(screen.queryByText(/switched off\./)).toBeNull();
  });
  it('lists class, scores with unknown as a dash, the checklist and reasons', async () => {
    mockApi.mockResolvedValue({ enabled: true, as_of: '2026-11-22', model_version: 'multibagger-0.1.0-provisional', counts: { watch: 1 }, disclaimer: 'Decision support, not investment advice.',
      items: [{ symbol: 'ABC', instrument_id: 'i1', class: 'watch', rank: null, scores: { Q: 72, G: 61, V: null, T: null, X: 55 }, checklist: '12 of 17 checks passed', timing: 'wait_for_chart', kind: 'default',
        reasons: ['capped: pledge'], failed_or_unknown: ['pledge: verify pledge'], class_if_unknown_checks_cleared: 'quality_compounder' }] });
    render(<Multibagger />);
    await waitFor(() => expect(screen.getByText('ABC')).toBeTruthy());
    expect(screen.getByText('12 of 17 checks passed')).toBeTruthy();
    expect(screen.getByText(/could reach Quality compounder/)).toBeTruthy();
    expect(screen.getAllByText('—').length).toBeGreaterThan(0);
  });
});
