import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { StockHero } from './StockHero';

afterEach(cleanup);
const base = { horizon: 'positional' as const, onHorizon: vi.fn(), busy: false, watched: false, onRun: vi.fn(), onRefresh: vi.fn(), onToggleWatch: vi.fn(), onJump: vi.fn() };
const inst = { id: 'i1', symbol: 'AAA', display_name: 'Alpha Ltd', venue: 'NSE', is_synthetic: false };

describe('StockHero when there is no report', () => {
  it('says the report could not be loaded, with the card research date and a Retry, never "No research report yet" for a researched card', () => {
    const onRetry = vi.fn();
    render(<StockHero {...base} instrument={{ ...inst, card_researched_at: '2026-11-22T11:03:00Z' }} brief={null} loadError="Failed to fetch" onRetry={onRetry} />);
    expect(screen.getByRole('alert').textContent).toContain('Could not load the report (network or server error)');
    expect(screen.getByRole('alert').textContent).toContain('Researched 22 Nov 2026, 16:33 IST');
    expect(screen.queryByText('No research report yet')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(onRetry).toHaveBeenCalled();
  });
  it('shows a loading line while the report loads, and "No research report yet" only for a stock never researched', () => {
    const { rerender } = render(<StockHero {...base} instrument={inst} brief={null} loading />);
    expect(screen.getByText('Loading the research report…')).toBeTruthy();
    expect(screen.queryByText('No research report yet')).toBeNull();
    rerender(<StockHero {...base} instrument={inst} brief={null} />);
    expect(screen.getByText('No research report yet')).toBeTruthy();
  });
});
