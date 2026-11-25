import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { LiveSwitch } from './LiveSwitch';

const { apiRequest } = vi.hoisted(() => ({ apiRequest: vi.fn() }));
vi.mock('../api', () => ({ apiRequest }));
vi.mock('../hooks/useExecutionState', () => ({ refreshExecutionState: vi.fn() }));

const venue = (armed: boolean) => ({ venue: 'dhan', armed, effective: { state: armed ? 'LIVE' : 'NOT_LIVE', reasons: [] }, strategies: [], window: { text: '' } }) as never;

afterEach(() => { cleanup(); apiRequest.mockReset(); });

describe('LiveSwitch unarm button', () => {
  it('is shown only while armed and disarms with one click', async () => {
    apiRequest.mockResolvedValue({});
    const { rerender } = render(<LiveSwitch venue={venue(false)} />);
    expect(screen.queryByRole('button', { name: /Unarm/ })).toBeNull();
    rerender(<LiveSwitch venue={venue(true)} />);
    fireEvent.click(screen.getByRole('button', { name: /Unarm .* live orders/ }));
    await waitFor(() => expect(apiRequest).toHaveBeenCalledWith('/dhan-live/disarm', expect.objectContaining({ method: 'POST' })));
  });
});
