import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { NeedsAttention } from './NeedsAttention';

afterEach(cleanup);

describe('NeedsAttention', () => {
  it('lists each item with its action, and the count', () => {
    const open = vi.fn();
    render(<NeedsAttention items={[{ id: 'a', tone: 'caution', text: '2 new alerts', action: { label: 'Open alerts', onClick: open } }, { id: 'b', tone: 'negative', text: 'Dhan session expired' }]} />);
    expect(screen.getByText('2')).toBeTruthy(); expect(screen.getByText('Act')).toBeTruthy(); expect(screen.getByText('Check')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Open alerts' })); expect(open).toHaveBeenCalled();
  });
  it('says all clear when there is nothing, and never says that while still loading', () => {
    const { rerender } = render(<NeedsAttention items={[]} />);
    expect(screen.getByText('all clear')).toBeTruthy(); expect(screen.getByText(/Nothing needs your attention/)).toBeTruthy();
    rerender(<NeedsAttention items={[]} loading />);
    expect(screen.queryByText('all clear')).toBeNull(); expect(screen.getByRole('status').getAttribute('aria-busy')).toBe('true');
  });
});
