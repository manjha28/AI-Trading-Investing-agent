import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { CryptoScanner } from './CryptoScanner';

const apiRequest = vi.fn();
vi.mock('../api', () => ({ apiRequest: (...a: unknown[]) => apiRequest(...a) }));

describe('CryptoScanner strategy picker', () => {
  beforeEach(() => { apiRequest.mockReset(); apiRequest.mockResolvedValue({ items: [] }); });

  it('offers the strategies and sends the chosen one', async () => {
    render(<CryptoScanner />);
    const select = await screen.findByLabelText('Scanner strategy');
    expect(Array.from((select as HTMLSelectElement).options).map((o) => o.value)).toEqual(['five_ema_alert', 'funding_extreme']);
    fireEvent.change(select, { target: { value: 'funding_extreme' } });
    expect((screen.getByLabelText('Scanner resolution') as HTMLSelectElement).disabled).toBe(true);       // defined on 1-hour bars only
    fireEvent.change(screen.getByLabelText('Symbols'), { target: { value: 'BTCUSD' } });
    fireEvent.click(screen.getByText('Start scanner on these'));
    await waitFor(() => expect(apiRequest).toHaveBeenCalledWith('/delta-scanner', expect.objectContaining({ method: 'POST' })));
    const body = JSON.parse((apiRequest.mock.calls.find((c) => c[1]?.method === 'POST')![1] as { body: string }).body);
    expect(body).toMatchObject({ strategy_key: 'funding_extreme', resolution: '1h', symbols: 'BTCUSD' });
  });
});
