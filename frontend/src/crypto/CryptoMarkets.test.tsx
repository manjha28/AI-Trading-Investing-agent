import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { CryptoMarkets } from './CryptoMarkets';

const api = vi.hoisted(() => ({ resolvers: [] as ((v: unknown) => void)[] }));
vi.mock('../api', () => ({ apiRequest: () => new Promise((resolve) => { api.resolvers.push(resolve); }) }));
vi.mock('./CryptoCharts', () => ({ CryptoCandleChart: () => <div>chart</div> }));
afterEach(() => { cleanup(); api.resolvers.length = 0; });

const props = { symbol: 'BTCUSD', setSymbol: () => undefined, resolution: '15m', setResolution: () => undefined };

describe('CryptoMarkets loading state', () => {
  it('shows loading placeholders while requests are in flight, never "No contracts match" or "No 15m candles stored"', () => {
    render(<CryptoMarkets {...props} />);
    expect(screen.getByText('Loading contracts…')).toBeTruthy();
    expect(screen.getByText('Loading 15m candles…')).toBeTruthy();
    expect(screen.queryByText(/No contracts match/)).toBeNull();
    expect(screen.queryByText(/candles stored/)).toBeNull();
  });

  it('shows the empty messages only after the responses arrive empty', async () => {
    render(<CryptoMarkets {...props} />);
    api.resolvers.forEach((resolve) => resolve({ items: [], count: 0, candles: [] }));
    await waitFor(() => expect(screen.getByText(/No contracts match/)).toBeTruthy());
    await waitFor(() => expect(screen.getByText(/No 15m candles stored for BTCUSD/)).toBeTruthy());
    expect(screen.queryByText('Loading contracts…')).toBeNull();
  });
});
