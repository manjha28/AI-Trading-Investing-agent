import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { sessionBanner } from '../lib/sessionExpiry';
import { DhanSessionBanner } from './DhanSessionBanner';

const mockApi = vi.hoisted(() => vi.fn());
vi.mock('../api', () => ({ apiRequest: mockApi }));
afterEach(() => { cleanup(); mockApi.mockReset(); });

const NOW = new Date('2026-11-22T10:00:00Z').getTime();

describe('sessionBanner', () => {
  it('warns when expired or under 60 minutes and stays quiet otherwise', () => {
    expect(sessionBanner('2026-11-22T09:59:00Z', NOW)?.level).toBe('expired');
    expect(sessionBanner('2026-11-22T10:45:00Z', NOW)).toEqual({ level: 'soon', text: expect.stringContaining('45 minutes') });
    expect(sessionBanner('2026-11-22T11:30:00Z', NOW)).toBeNull();
    expect(sessionBanner(null, NOW)).toBeNull();
    expect(sessionBanner('garbage', NOW)).toBeNull();
  });
});

describe('DhanSessionBanner', () => {
  it('shows the expiry in IST and a link to the Dhan connection page when the session is about to expire', async () => {
    mockApi.mockResolvedValue([{ slug: 'angel_one', status: 'expired', expires_at: null }, { slug: 'dhan', status: 'connected', expires_at: '2026-11-22T10:30:00Z' }]);
    const open = vi.fn();
    render(<DhanSessionBanner allowed onOpenBrokers={open} now={NOW} />);
    await waitFor(() => expect(screen.getByTestId('dhan-session-expiry').textContent).toContain('22 Nov 2026, 16:00 IST'));
    expect(screen.getByRole('alert').textContent).toContain('expires in 30 minutes');
    fireEvent.click(screen.getByRole('button', { name: 'Open Dhan connection' }));
    expect(open).toHaveBeenCalled();
  });
  it('does not call the API until the session is verified (no 403 before two-factor)', () => {
    render(<DhanSessionBanner allowed={false} onOpenBrokers={vi.fn()} now={NOW} />);
    expect(mockApi).not.toHaveBeenCalled();
  });
  it('shows no warning for a session with plenty of time left', async () => {
    mockApi.mockResolvedValue([{ slug: 'dhan', status: 'connected', expires_at: '2026-11-23T10:00:00Z' }]);
    render(<DhanSessionBanner allowed onOpenBrokers={vi.fn()} now={NOW} />);
    await waitFor(() => expect(screen.getByTestId('dhan-session-expiry')).toBeTruthy());
    expect(screen.queryByRole('alert')).toBeNull();
  });
});
