import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { useState } from 'react';
import { CodeInput } from './kit';
import { DhanStatusRow } from '../DhanStatusRow';
import { SlowBackendBanner } from '../SlowBackendBanner';

const api = vi.hoisted(() => ({ request: vi.fn(), listeners: new Set<(n: number) => void>() }));
vi.mock('../../api', () => ({ apiRequest: api.request, subscribeSlowRequests: (l: (n: number) => void) => { api.listeners.add(l); l(0); return () => api.listeners.delete(l); } }));
afterEach(() => { cleanup(); api.request.mockReset(); api.listeners.clear(); });

function Host({ initial = '' }: { initial?: string }) { const [v, setV] = useState(initial); return <><CodeInput value={v} onChange={setV} /><output data-testid="out">{v}</output></>; }

describe('CodeInput', () => {
  it('advances on each digit, ignores letters, and Backspace steps back', () => {
    render(<Host />);
    const boxes = screen.getAllByRole('textbox');
    expect(boxes).toHaveLength(6);
    fireEvent.change(boxes[0], { target: { value: '4' } });
    expect(document.activeElement).toBe(boxes[1]);
    fireEvent.change(boxes[1], { target: { value: 'x' } });
    expect(screen.getByTestId('out').textContent).toBe('4');
    fireEvent.change(boxes[1], { target: { value: '7' } });
    expect(screen.getByTestId('out').textContent).toBe('47');
    fireEvent.keyDown(boxes[2], { key: 'Backspace' });
    expect(document.activeElement).toBe(boxes[1]); expect(screen.getByTestId('out').textContent).toBe('4');
  });
  it('fills all six boxes from a pasted code and exposes numeric input mode', () => {
    render(<Host />);
    const boxes = screen.getAllByRole('textbox');
    fireEvent.paste(boxes[0], { clipboardData: { getData: () => '123 456' } });
    expect(screen.getByTestId('out').textContent).toBe('123456');
    expect(boxes.every((b) => b.getAttribute('inputmode') === 'numeric')).toBe(true);
  });
});

describe('DhanStatusRow', () => {
  const now = Date.parse('2026-11-23T10:00:00Z');
  const show = async (row: unknown) => { api.request.mockResolvedValue(row === null ? [] : [row]); render(<DhanStatusRow allowed onOpen={() => undefined} now={now} />); return screen.findByRole('button'); };
  it('says how long the session has left, with a tone: ok, under 60 minutes, expired, not connected', async () => {
    expect((await show({ slug: 'dhan', status: 'connected', expires_at: '2026-11-23T15:00:00Z' })).textContent).toContain('expires in 5h');
    cleanup(); expect((await show({ slug: 'dhan', status: 'connected', expires_at: '2026-11-23T10:30:00Z' })).className).toContain('dhan-status-warn');
    cleanup(); expect((await show({ slug: 'dhan', status: 'connected', expires_at: '2026-11-23T09:00:00Z' })).textContent).toContain('session expired');
    cleanup(); expect((await show(null)).textContent).toContain('not connected');
  });
  it('shows nothing until broker data may be read, and says expiry unknown rather than guessing', async () => {
    const { container } = render(<DhanStatusRow allowed={false} onOpen={() => undefined} now={now} />);
    expect(container.textContent).toBe('');
    cleanup(); expect((await show({ slug: 'dhan', status: 'connected', expires_at: null })).textContent).toContain('expiry unknown');
  });
});

describe('SlowBackendBanner', () => {
  it('appears only while a request has been pending for more than 8 seconds', () => {
    render(<SlowBackendBanner />);
    expect(screen.queryByText(/Backend is slow/)).toBeNull();
    act(() => api.listeners.forEach((l) => l(2)));
    expect(screen.getByText(/Backend is slow. Still loading 2 requests/)).toBeTruthy();
    act(() => api.listeners.forEach((l) => l(0)));
    expect(screen.queryByText(/Backend is slow/)).toBeNull();
  });
});
