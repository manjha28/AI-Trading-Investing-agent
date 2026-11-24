import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { NeedsAttention, type AttentionItem } from './NeedsAttention';

afterEach(cleanup);
const item = (n: number, over: Partial<AttentionItem> = {}): AttentionItem => ({ id: `i${n}`, tone: 'caution', text: `Item ${n}`, ...over });

describe('NeedsAttention', () => {
  it('is one compact row per item with icon, text, count and a link', () => {
    const open = vi.fn();
    render(<NeedsAttention items={[item(1, { count: 2, action: { label: 'Open alerts', onClick: open } }), item(2, { tone: 'negative' })]} />);
    expect(screen.getAllByRole('listitem')).toHaveLength(2);
    expect(screen.getByLabelText('Check')).toBeTruthy(); expect(screen.getByLabelText('Act')).toBeTruthy();
    expect(screen.getByText('2', { selector: '.na-count' })).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Open alerts' })); expect(open).toHaveBeenCalled();
  });
  it('shows at most five rows and the rest on "Show all"', () => {
    render(<NeedsAttention items={[1, 2, 3, 4, 5, 6, 7].map((n) => item(n))} />);
    expect(screen.getAllByRole('listitem')).toHaveLength(5);
    fireEvent.click(screen.getByRole('button', { name: 'Show all 7' }));
    expect(screen.getAllByRole('listitem')).toHaveLength(7);
    fireEvent.click(screen.getByRole('button', { name: 'Show fewer' }));
    expect(screen.getAllByRole('listitem')).toHaveLength(5);
  });
  it('renders nothing when nothing needs attention, and nothing while still loading', () => {
    const { container, rerender } = render(<NeedsAttention items={[]} />);
    expect(container.innerHTML).toBe('');
    rerender(<NeedsAttention items={[item(1)]} loading />);
    expect(container.innerHTML).toBe('');
  });
});
