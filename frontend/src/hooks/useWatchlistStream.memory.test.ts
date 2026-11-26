// Test data only: a fake socket and hand-written ticks.
import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { clearUniverseQuoteMemory, useWatchlistStream } from './useWatchlistStream';

vi.mock('../api', () => ({ apiRequest: vi.fn(async () => ({})) }));

class FakeSocket {
  static OPEN = 1;
  static last: FakeSocket | null = null;
  readyState = 1;
  onopen: (() => void) | null = null;
  onmessage: ((e: { data: string }) => void) | null = null;
  onclose: ((e: unknown) => void) | null = null;
  onerror: (() => void) | null = null;
  constructor() { FakeSocket.last = this; }
  send() {}
  close() {}
}

const tick = (at: string) => JSON.stringify({ type: 'tick', instrument_id: 'i1', security_id: '1', quote: { bar: { close: '123.45' }, source: 'dhan_ws', market_timestamp: at, market_age_seconds: 0, freshness_status: 'live', is_synthetic: false, is_verified: true } });

describe('universe quotes survive leaving the page', () => {
  beforeEach(() => { vi.stubGlobal('WebSocket', FakeSocket); clearUniverseQuoteMemory(); FakeSocket.last = null; });
  afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

  async function receive(at: string) {
    const first = renderHook(() => useWatchlistStream({ universe: true }));
    await waitFor(() => expect(FakeSocket.last).not.toBeNull());
    act(() => { FakeSocket.last!.onopen?.(); FakeSocket.last!.onmessage?.({ data: tick(at) }); });
    await waitFor(() => expect(first.result.current.quotes.i1?.close).toBe('123.45'), { timeout: 3000 });
    first.unmount();
  }

  it("shows today's last tick again on return, marked as last seen and not live", async () => {
    await receive(new Date().toISOString());
    const back = renderHook(() => useWatchlistStream({ universe: true }));
    await waitFor(() => expect(back.result.current.quotes.i1?.close).toBe('123.45'));
    expect(back.result.current.quotes.i1.freshness_status).toBe('last_seen');
  });

  it('does not bring back a tick from an earlier day', async () => {
    await receive(new Date(Date.now() - 3 * 86_400_000).toISOString());
    const back = renderHook(() => useWatchlistStream({ universe: true }));
    expect(back.result.current.quotes.i1).toBeUndefined();
  });
});
