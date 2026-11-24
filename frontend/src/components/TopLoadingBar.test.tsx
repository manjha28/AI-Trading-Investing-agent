import { act, cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { TopLoadingBar } from './TopLoadingBar';

const api = vi.hoisted(() => ({ listeners: new Set<(n: number) => void>() }));
vi.mock('../api', () => ({ subscribePending: (l: (n: number) => void) => { api.listeners.add(l); l(0); return () => api.listeners.delete(l); } }));
afterEach(() => { cleanup(); vi.useRealTimers(); api.listeners.clear(); });
const set = (n: number) => act(() => api.listeners.forEach((l) => l(n)));

describe('TopLoadingBar', () => {
  it('stays hidden for quick requests, shows after 400 ms of waiting, and disappears when done', () => {
    vi.useFakeTimers();
    render(<TopLoadingBar />);
    set(1); act(() => { vi.advanceTimersByTime(300); });
    expect(screen.queryByRole('progressbar')).toBeNull();
    act(() => { vi.advanceTimersByTime(150); });
    expect(screen.getByRole('progressbar', { name: 'Loading' })).toBeTruthy();
    set(0); expect(screen.queryByRole('progressbar')).toBeNull();
  });
});
