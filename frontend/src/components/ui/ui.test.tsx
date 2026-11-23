import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { Banner, Button, Chip, DataTable, EmptyState, ErrorState, ScoreBar, Sheet, Skeleton, StaleBadge, Stat, STATUS_TONE, Tabs, toneOf, windowRange, type Column } from './kit';
import { useState } from 'react';

afterEach(cleanup);

describe('status chips', () => {
  it('maps every domain word to exactly one tone and renders that tone class', () => {
    const words = ['Buy', 'Hold', 'Wait', 'Avoid', 'Radar', 'Stale', 'Delayed', 'Live', 'Closed', 'Unverified', 'Unknown', 'compounder', 'emerging', 'watch', 'not_a_fit', 'not_tradable'];
    for (const w of words) {
      expect(STATUS_TONE[w.toLowerCase()], w).toBeDefined();
      const { container, unmount } = render(<Chip status={w} />);
      expect(container.querySelector(`.ui-tone-${toneOf(w)}`), w).toBeTruthy();
      unmount();
    }
    expect([toneOf('Buy'), toneOf('Avoid'), toneOf('Wait'), toneOf('Hold'), toneOf('whatever')]).toEqual(['positive', 'negative', 'caution', 'info', 'neutral']);
  });
  it('pairs colour with a glyph and a word, so colour is never the only signal', () => {
    const { container } = render(<Chip status="avoid" />);
    expect(container.textContent).toContain('Avoid');
    expect(container.querySelector('.ui-chip-dot')?.textContent).toBeTruthy();
  });
});

describe('Button', () => {
  it('requires an accessible name when it has no text, and blocks clicks while loading', () => {
    expect(() => render(<Button />)).toThrow(/aria-label/);
    const onClick = vi.fn();
    render(<Button loading onClick={onClick}>Save</Button>);
    const b = screen.getByRole('button', { name: /Save/ });
    expect((b as HTMLButtonElement).disabled).toBe(true); expect(b.getAttribute('aria-busy')).toBe('true');
    fireEvent.click(b); expect(onClick).not.toHaveBeenCalled();
  });
});

type Row = { id: string; name: string; q: number | null };
const cols: Column<Row>[] = [
  { key: 'name', header: 'Name', render: (r) => r.name, sortValue: (r) => r.name, sticky: true },
  { key: 'q', header: 'Q', align: 'right', render: (r) => (r.q === null ? 'n/a' : r.q), sortValue: (r) => r.q, hideable: true },
];
const many = (n: number): Row[] => Array.from({ length: n }, (_, i) => ({ id: `r${i}`, name: `Stock ${String(i).padStart(3, '0')}`, q: i % 10 === 0 ? null : i }));

describe('DataTable', () => {
  it('sorts on header click with aria-sort, and keeps unknown values last in both directions', () => {
    render(<DataTable caption="t" columns={cols} rows={many(12)} rowKey={(r) => r.id} />);
    const header = screen.getByRole('columnheader', { name: /Q/ });
    expect(header.getAttribute('aria-sort')).toBe('none');
    fireEvent.click(within(header).getByRole('button'));                                  // numeric columns start descending
    expect(header.getAttribute('aria-sort')).toBe('descending');
    let rows = screen.getAllByRole('row').slice(1);
    expect(rows[0].textContent).toContain('11');
    expect(rows[rows.length - 1].textContent).toContain('n/a');
    fireEvent.click(within(header).getByRole('button'));
    expect(header.getAttribute('aria-sort')).toBe('ascending');
    rows = screen.getAllByRole('row').slice(1);
    expect(rows[0].textContent).toContain('1');
    expect(rows[rows.length - 1].textContent).toContain('n/a');                           // unknown is last ascending too
  });
  it('renders only the visible window above 100 rows', () => {
    render(<DataTable caption="big" columns={cols} rows={many(500)} rowKey={(r) => r.id} viewportHeight={560} />);
    const rendered = document.querySelectorAll('tr[data-row]').length;
    expect(rendered).toBeGreaterThan(10); expect(rendered).toBeLessThanOrEqual(60);
    expect(screen.getByRole('table').getAttribute('aria-rowcount')).toBe('500');
  });
  it('windows by scroll position', () => {
    expect(windowRange(500, 0, 560, 40)).toEqual({ start: 0, end: 22 });
    expect(windowRange(500, 4000, 560, 40).start).toBe(92);
    expect(windowRange(10, 0, 560, 40)).toEqual({ start: 0, end: 10 });
  });
  it('hides a column from the menu, opens a row on click or Enter, and moves with the arrow keys', () => {
    const open = vi.fn();
    render(<DataTable caption="t" columns={cols} rows={many(5)} rowKey={(r) => r.id} onRowClick={open} />);
    fireEvent.click(screen.getByLabelText('Q'));
    expect(screen.queryByRole('columnheader', { name: /^Q/ })).toBeNull();
    const rows = () => screen.getAllByRole('row').slice(1);
    rows()[0].focus(); fireEvent.keyDown(rows()[0], { key: 'ArrowDown' });
    expect(document.activeElement).toBe(rows()[1]);
    fireEvent.keyDown(rows()[1], { key: 'Enter' }); expect(open).toHaveBeenCalledWith(many(5)[1]);
  });
  it('shows a skeleton while loading, a real error with Retry, and an empty sentence', () => {
    const retry = vi.fn();
    const { rerender } = render(<DataTable caption="t" columns={cols} rows={[]} rowKey={(r) => r.id} loading />);
    expect(screen.getByRole('status', { name: 'Loading' })).toBeTruthy();
    rerender(<DataTable caption="t" columns={cols} rows={[]} rowKey={(r) => r.id} error={new Error('HTTP 502')} onRetry={retry} />);
    expect(screen.getByRole('alert').textContent).toContain('HTTP 502');
    fireEvent.click(screen.getByRole('button', { name: 'Retry' })); expect(retry).toHaveBeenCalled();
    rerender(<DataTable caption="t" columns={cols} rows={[]} rowKey={(r) => r.id} empty="No stocks match these filters." />);
    expect(screen.getByText('No stocks match these filters.')).toBeTruthy();
  });
});

describe('ScoreBar, StaleBadge, Stat, states', () => {
  it('shows n/a with its reason for unknown scores, and never draws unknown as an empty bar', () => {
    render(<><ScoreBar value={null} label="Quality" naReason="No filings yet" /><ScoreBar value={82} label="Growth" /><ScoreBar value={0} label="Valuation" /></>);
    expect(screen.getByLabelText(/Quality: not available. No filings yet/)).toBeTruthy();
    expect(screen.getByLabelText('Growth: 82 out of 100')).toBeTruthy();
    expect(screen.getByLabelText('Valuation: 0 out of 100')).toBeTruthy();
  });
  it('flags stale data with its age, stays silent when fresh, and says unknown when the time is unknown', () => {
    const now = Date.parse('2026-11-23T12:00:00Z');
    const { container, rerender } = render(<StaleBadge asOf="2026-11-23T11:55:00Z" staleAfterMinutes={15} now={now} />);
    expect(container.textContent).toBe('');
    rerender(<StaleBadge asOf="2026-11-23T09:00:00Z" staleAfterMinutes={15} now={now} />);
    expect(container.textContent).toContain('Stale 3h');
    rerender(<StaleBadge asOf={null} staleAfterMinutes={15} now={now} unknownLabel="No as-of time" />);
    expect(container.textContent).toContain('No as-of time');
  });
  it('renders a stat with an arrowed delta and one footer line', () => {
    render(<Stat label="Day change" value="₹1,200.00" delta={-1.4} asOf="2026-11-23T10:00:00Z" source="Dhan" />);
    expect(screen.getByText('▼ -1.40%')).toBeTruthy();
    expect(screen.getByText(/as of .* IST · Dhan/)).toBeTruthy();
  });
  it('banners use alert for errors and status otherwise; skeleton and empty are announced', () => {
    render(<><Banner tone="error">Broken</Banner><Banner>Note</Banner><Skeleton variant="card" /><EmptyState>Nothing here.</EmptyState><ErrorState error="boom" /></>);
    expect(screen.getAllByRole('alert').length).toBe(2);
    expect(screen.getAllByRole('status').length).toBeGreaterThanOrEqual(3);
  });
});

describe('Tabs and Sheet', () => {
  it('uses a roving tabindex and arrow keys', () => {
    function Host() { const [a, setA] = useState('one'); return <Tabs label="x" active={a} onChange={setA} items={[{ id: 'one', label: 'One' }, { id: 'two', label: 'Two', count: 3 }]} />; }
    render(<Host />);
    const [one, two] = screen.getAllByRole('tab');
    expect(one.tabIndex).toBe(0); expect(two.tabIndex).toBe(-1);
    fireEvent.keyDown(one, { key: 'ArrowRight' });
    expect(two.getAttribute('aria-selected')).toBe('true'); expect(document.activeElement).toBe(two);
    fireEvent.keyDown(two, { key: 'ArrowRight' }); expect(one.getAttribute('aria-selected')).toBe('true');
  });
  it('is a modal dialog that traps Tab, closes on Escape and returns focus to the opener', () => {
    function Host() { const [o, setO] = useState(false); return <><button onClick={() => setO(true)}>open</button><Sheet open={o} onClose={() => setO(false)} title="Detail"><button>inside</button></Sheet></>; }
    render(<Host />);
    const opener = screen.getByText('open'); opener.focus(); fireEvent.click(opener);
    const dialog = screen.getByRole('dialog', { name: 'Detail' });
    expect(dialog.getAttribute('aria-modal')).toBe('true');
    const buttons = within(dialog).getAllByRole('button');
    buttons[buttons.length - 1].focus(); fireEvent.keyDown(document, { key: 'Tab' });
    expect(document.activeElement).toBe(buttons[0]);
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(screen.queryByRole('dialog')).toBeNull(); expect(document.activeElement).toBe(opener);
  });
});
