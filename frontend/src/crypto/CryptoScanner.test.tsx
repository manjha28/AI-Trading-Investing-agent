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
    expect(Array.from((select as HTMLSelectElement).options).map((o) => o.value)).toEqual(['five_ema_alert', 'funding_extreme', 'trend_donchian_4h', 'momentum_56d', 'rsi2_daily']);
    fireEvent.change(select, { target: { value: 'funding_extreme' } });
    expect((screen.getByLabelText('Scanner resolution') as HTMLSelectElement).disabled).toBe(true);       // defined on 1-hour bars only
    fireEvent.change(screen.getByLabelText('Symbols'), { target: { value: 'BTCUSD' } });
    fireEvent.click(screen.getByText('Start scanner on these'));
    await waitFor(() => expect(apiRequest).toHaveBeenCalledWith('/delta-scanner', expect.objectContaining({ method: 'POST' })));
    const body = JSON.parse((apiRequest.mock.calls.find((c) => c[1]?.method === 'POST')![1] as { body: string }).body);
    expect(body).toMatchObject({ strategy_key: 'funding_extreme', resolution: '1h', symbols: 'BTCUSD' });
  });
});

describe('CryptoScanner live switch', () => {
  beforeEach(() => { apiRequest.mockReset(); });

  it('is off by default, inert with a reason for other strategies, and the kill switch posts', async () => {
    apiRequest.mockImplementation(async (path: string) => (path === '/delta-scanner' ? { items: [
      { id: 'a', name: 'EMA scan', strategy_key: 'five_ema_alert', resolution: '5m', symbols: ['BTCUSD'], status: 'active', label: 'paper', wallet_usd: '1000', total_net_pnl: null, per_symbol: null, signals_seen: 0, signals_taken: 0, last_run_at: null, last_error: null, execute_live: false, live_block: null },
      { id: 'b', name: 'Funding scan', strategy_key: 'funding_extreme', resolution: '1h', symbols: ['BTCUSD'], status: 'active', label: 'unproven', wallet_usd: '1000', total_net_pnl: null, per_symbol: null, signals_seen: 0, signals_taken: 0, last_run_at: null, last_error: null, execute_live: false, live_block: "'funding_extreme' is not on the live allowlist" },
    ] } : {}));
    render(<CryptoScanner />);
    const sw = await screen.findByRole('button', { name: 'EMA scan live switch' });
    expect(sw.textContent).toBe('Live off');
    expect(screen.queryByRole('button', { name: 'Funding scan live switch' })).toBeNull();
    expect(screen.getByText(/not on the live allowlist/)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Kill switch for EMA scan' }));
    await waitFor(() => expect(apiRequest).toHaveBeenCalledWith('/delta-scanner/a/live/kill', expect.objectContaining({ method: 'POST' })));
    expect(apiRequest).not.toHaveBeenCalledWith('/delta-scanner/a/live', expect.anything());
  });
});
