import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { useState } from 'react';
import { Card, FilterBar, Meter, SearchBox, Select, Sparkline, ToastProvider, Tooltip, useToast } from './kit';

afterEach(() => { cleanup(); vi.useRealTimers(); });

describe('SearchBox', () => {
  it('focuses on "/" outside a text field and ignores "/" while typing in one', () => {
    function Host() { const [v, setV] = useState(''); return <><input aria-label="other" /><SearchBox value={v} onChange={setV} label="Search stocks" /></>; }
    render(<Host />);
    fireEvent.keyDown(document.body, { key: '/' });
    expect(document.activeElement).toBe(screen.getByLabelText('Search stocks'));
    const other = screen.getByLabelText('other'); other.focus(); fireEvent.keyDown(other, { key: '/' });
    expect(document.activeElement).toBe(other);
  });
});

describe('FilterBar and Select', () => {
  it('toggles chips with counts and shows Clear all only when something is on', () => {
    const toggle = vi.fn(); const clear = vi.fn();
    const chips = [{ id: 'held', label: 'Held', count: 3 }, { id: 'band', label: 'In band', count: 1 }];
    const { rerender } = render(<FilterBar chips={chips} active={[]} onToggle={toggle} onClear={clear} />);
    expect(screen.queryByText('Clear all')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: /Held/ })); expect(toggle).toHaveBeenCalledWith('held');
    rerender(<FilterBar chips={chips} active={['held']} onToggle={toggle} onClear={clear} />);
    expect(screen.getByRole('button', { name: /Held/ }).getAttribute('aria-pressed')).toBe('true');
    fireEvent.click(screen.getByText('Clear all')); expect(clear).toHaveBeenCalled();
  });
  it('selects an option', () => {
    const change = vi.fn();
    render(<Select label="Sector" value="" onChange={change} options={[{ value: '', label: 'All' }, { value: 'it', label: 'IT' }]} />);
    fireEvent.change(screen.getByLabelText('Sector'), { target: { value: 'it' } }); expect(change).toHaveBeenCalledWith('it');
  });
});

describe('Tooltip, Card, Sparkline, Meter', () => {
  it('describes its target with the tooltip text', () => {
    render(<Tooltip text="Why this is n/a"><span>n/a</span></Tooltip>);
    const trigger = screen.getByText('n/a').closest('.ui-tip') as HTMLElement;
    expect(screen.getByRole('tooltip', { hidden: true }).textContent).toBe('Why this is n/a');
    expect(trigger.getAttribute('aria-describedby')).toBe(screen.getByRole('tooltip', { hidden: true }).id);
  });
  it('draws a sparkline with gaps for unknowns and nothing for too little data', () => {
    const { container, rerender } = render(<Sparkline values={[1, 2, null, 4, 3]} />);
    expect(container.querySelectorAll('path').length).toBe(2);                                    // the null splits the line, it is not bridged or zeroed
    expect(screen.getByRole('img').getAttribute('aria-label')).toContain('up');
    rerender(<Sparkline values={[5, null, null]} />);
    expect(container.querySelector('svg')).toBeNull(); expect(container.textContent).toBe('—');
  });
  it('shows a meter value, and n/a for unknown rather than an empty bar', () => {
    const { rerender } = render(<Meter value={72.4} label="Confidence" />);
    expect(screen.getByRole('meter').getAttribute('aria-valuenow')).toBe('72');
    rerender(<Meter value={null} label="Confidence" />);
    expect(screen.queryByRole('meter')).toBeNull(); expect(screen.getByLabelText('Confidence: not available')).toBeTruthy();
  });
  it('renders a titled card', () => { render(<Card title="Levels">body</Card>); expect(screen.getByRole('heading', { name: 'Levels' })).toBeTruthy(); });
});

describe('Toast', () => {
  it('announces in a live region, closes info toasts after 6 seconds and keeps errors until dismissed', () => {
    vi.useFakeTimers();
    function Host() { const toast = useToast(); return <><button onClick={() => toast('Saved')}>ok</button><button onClick={() => toast('Sync failed', 'negative')}>bad</button></>; }
    const { container } = render(<ToastProvider><Host /></ToastProvider>);
    expect(container.querySelector('[aria-live="polite"]')).toBeTruthy();
    fireEvent.click(screen.getByText('ok')); fireEvent.click(screen.getByText('bad'));
    expect(screen.getByRole('status').textContent).toContain('Saved'); expect(screen.getByRole('alert').textContent).toContain('Sync failed');
    act(() => { vi.advanceTimersByTime(6100); });
    expect(screen.queryByText(/Saved/)).toBeNull(); expect(screen.getByText(/Sync failed/)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss' })); expect(screen.queryByText(/Sync failed/)).toBeNull();
  });
});
