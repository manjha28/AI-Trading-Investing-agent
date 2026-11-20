import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useCommodityStream } from './useCommodityStream';

class FakeSocket {
  static instances: FakeSocket[] = [];
  static OPEN = 1;
  readyState = 1;
  sent: string[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onclose: ((event: { code: number }) => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;
  constructor(public url: URL) { FakeSocket.instances.push(this); }
  send(data: string) { this.sent.push(data); }
  close() { this.closed = true; this.onclose?.({ code: 1000 }); }
  emit(payload: unknown) { this.onmessage?.({ data: JSON.stringify(payload) }); }
}

const tickMessage = (key: string, ltp: string) => ({ type: 'tick', key, symbol: key.toUpperCase(), ltp, previous_close: '100', change: '1', change_pct: '1.00', open: '1', high: '2', low: '1', volume: '5', received_at: '2026-11-20T15:20:00Z' });

beforeEach(() => {
  vi.useFakeTimers();
  FakeSocket.instances = [];
  vi.stubGlobal('WebSocket', FakeSocket as unknown as typeof WebSocket);
});
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });

describe('useCommodityStream', () => {
  it('connects to the commodity stream, batches ticks, and keeps only the newest per contract', async () => {
    const { result } = renderHook(() => useCommodityStream(true));
    const socket = FakeSocket.instances[0];
    expect(socket.url.pathname).toBe('/api/v1/market-data/commodities/stream');
    expect(socket.url.protocol).toBe('ws:');
    expect(result.current.status).toBe('connecting');
    act(() => { socket.onopen?.(); socket.emit({ type: 'status', status: 'active', message: null }); });
    expect(result.current.status).toBe('active');
    act(() => {
      socket.emit(tickMessage('mcx:1', '100'));
      socket.emit(tickMessage('mcx:1', '101'));   // newer tick for the same contract within one batch
      socket.emit(tickMessage('mcx:2', '50'));
    });
    expect(Object.keys(result.current.ticks)).toEqual([]);   // batched: nothing yet
    act(() => { vi.advanceTimersByTime(200); });
    expect(result.current.ticks['mcx:1'].ltp).toBe('101');
    expect(result.current.ticks['mcx:2'].ltp).toBe('50');
    expect(result.current.ticks['mcx:1'].receivedAt).toBeGreaterThan(0);
  });

  it('keeps the socket alive with pings and keeps a "market closed" notice from being overwritten by packets', () => {
    const { result } = renderHook(() => useCommodityStream(true));
    const socket = FakeSocket.instances[0];
    act(() => { socket.onopen?.(); socket.emit({ type: 'status', status: 'market_closed', message: 'MCX is closed' }); });
    act(() => { vi.advanceTimersByTime(15_100); });
    expect(socket.sent).toContain('ping');
    act(() => { socket.emit(tickMessage('mcx:1', '100')); socket.emit({ type: 'status', status: 'active', message: null }); vi.advanceTimersByTime(200); });
    expect(result.current.status).toBe('market_closed');
    expect(result.current.message).toBeNull(); // (the later status message replaced the text, but the closed state holds)
  });

  it('reconnects with backoff after a dropped connection', () => {
    const { result } = renderHook(() => useCommodityStream(true));
    act(() => { FakeSocket.instances[0].onopen?.(); FakeSocket.instances[0].onclose?.({ code: 1006 }); });
    expect(result.current.status).toBe('reconnecting');
    expect(FakeSocket.instances).toHaveLength(1);
    act(() => { vi.advanceTimersByTime(1_000); });
    expect(FakeSocket.instances).toHaveLength(2);
    act(() => { FakeSocket.instances[1].onclose?.({ code: 1006 }); });
    act(() => { vi.advanceTimersByTime(1_000); });
    expect(FakeSocket.instances).toHaveLength(2);   // second wait is longer (2s)
    act(() => { vi.advanceTimersByTime(1_000); });
    expect(FakeSocket.instances).toHaveLength(3);
  });

  it('does not retry when the server refuses the session, and says so', () => {
    const { result } = renderHook(() => useCommodityStream(true));
    act(() => { FakeSocket.instances[0].onclose?.({ code: 4403 }); });
    expect(result.current.status).toBe('unavailable');
    expect(result.current.message).toMatch(/verified Dhan session/);
    act(() => { vi.advanceTimersByTime(120_000); });
    expect(FakeSocket.instances).toHaveLength(1);   // retrying cannot change a refused session
  });

  it('does nothing when disabled and closes the socket when unmounted', () => {
    const disabled = renderHook(() => useCommodityStream(false));
    expect(disabled.result.current.status).toBe('idle');
    expect(FakeSocket.instances).toHaveLength(0);
    const live = renderHook(() => useCommodityStream(true));
    const socket = FakeSocket.instances[0];
    live.unmount();
    expect(socket.closed).toBe(true);
    act(() => { vi.advanceTimersByTime(60_000); });
    expect(FakeSocket.instances).toHaveLength(1);   // no reconnect after unmount
  });
});
