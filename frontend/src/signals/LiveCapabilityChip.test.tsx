import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { LiveCapabilityChip } from './LiveCapabilityChip';

const api = vi.hoisted(() => vi.fn());
vi.mock('../api', () => ({ apiRequest: api }));
afterEach(() => { cleanup(); api.mockReset(); });

describe('LiveCapabilityChip', () => {
  it('says paper only when live execution is off, available when on but unarmed, and ARMED when armed', async () => {
    api.mockResolvedValue({ deployment_enabled: false, armed: false });
    const { unmount } = render(<LiveCapabilityChip />); expect(await screen.findByText(/Paper only: Dhan live orders off/)).toBeTruthy(); unmount();
    api.mockResolvedValue({ deployment_enabled: true, armed: false });
    const second = render(<LiveCapabilityChip />); expect(await screen.findByText(/not armed/)).toBeTruthy(); second.unmount();
    api.mockResolvedValue({ deployment_enabled: true, armed: true });
    render(<LiveCapabilityChip />); expect(await screen.findByText(/Live orders ARMED/)).toBeTruthy();
  });
  it('never claims paper-only when the server could not say', async () => {
    api.mockRejectedValue(new Error('down'));
    render(<LiveCapabilityChip />);
    expect(await screen.findByText('Order capability unknown')).toBeTruthy();
  });
});
