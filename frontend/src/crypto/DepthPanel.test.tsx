import { act, cleanup, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { DepthPanel, groupLevels, type Depth } from './DepthPanel';

// TEST DATA: a fake socket and hand-written book; nothing here reaches Delta.
class FakeSocket {
  static all: FakeSocket[] = [];
  onopen: (() => void) | null = null; onmessage: ((e: { data: string }) => void) | null = null; onclose: ((e: { code: number }) => void) | null = null;
  closed = false;
  constructor(public url: URL) { FakeSocket.all.push(this); }
  close() { this.closed = true; }
  send() { /* heartbeat not used */ }
  emit(d: unknown) { this.onmessage?.({ data: JSON.stringify(d) }); }
}
const depth = (over: Partial<Depth> = {}): Depth => ({
  type: 'depth', symbol: 'BTCUSD', stale: false, age_seconds: 0.1, provisional: false, gaps: 0, resyncs: 0,
  bids: [{ price: '100', size: '5', cum: '5' }, { price: '99.5', size: '10', cum: '15' }], asks: [{ price: '100.5', size: '4', cum: '4' }, { price: '101', size: '8', cum: '12' }],
  metrics: { best_bid: '100', best_ask: '100.5', mid: '100.25', spread: '0.5', spread_bps: '49.88', imbalance_5: '0.1111', imbalance_20: '0.1111' },
  trades: [{ price: '100.5', size: '3', aggressor: 'buy', time: '1' }, { price: '100', size: '2', aggressor: 'sell', time: '2' }],
  mark: { mark_price: '100.2', index_price: '100.1', funding_rate: '0.01', open_interest_usd: '1000000' }, funding_countdown_seconds: 3725, funding_schedule: 'assumed', disclaimer: 'Decision support, not investment advice.', ...over,
});
beforeEach(() => { FakeSocket.all = []; vi.stubGlobal('WebSocket', FakeSocket); vi.useFakeTimers(); });
afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); });

describe('DepthPanel', () => {
  it('opens one socket for the symbol, shows LIVE with a book, trades coloured by aggressor, mark/index/funding/OI', async () => {
    render(<DepthPanel symbol="BTCUSD" onClose={() => undefined} />);
    expect(FakeSocket.all).toHaveLength(1);
    expect(FakeSocket.all[0].url.pathname).toBe('/api/v1/delta-depth/BTCUSD/stream');
    act(() => FakeSocket.all[0].emit(depth()));
    expect(screen.getByRole('status').textContent).toBe('LIVE');
    expect(screen.getByText(/spread 0.5/).textContent).toContain('49.88 bps');
    expect(screen.getAllByText('+11.1% bid-heavy', { exact: false })).toHaveLength(2);   // top 5 and top 20
    expect(document.querySelectorAll('.depth-tape li.positive')).toHaveLength(1);
    expect(document.querySelectorAll('.depth-tape li.negative')).toHaveLength(1);
    expect(screen.getByText(/in 1h 02m 05s/)).toBeTruthy();
  });

  it('turns STALE after 3 seconds without an update and LIVE again on the next one', async () => {
    render(<DepthPanel symbol="BTCUSD" onClose={() => undefined} />);
    act(() => FakeSocket.all[0].emit(depth()));
    await act(async () => { await vi.advanceTimersByTimeAsync(2_000); });
    expect(screen.getByRole('status').textContent).toBe('LIVE');
    await act(async () => { await vi.advanceTimersByTimeAsync(2_000); });
    expect(screen.getByRole('status').textContent).toBe('STALE');
    act(() => FakeSocket.all[0].emit(depth()));
    expect(screen.getByRole('status').textContent).toBe('LIVE');
  });

  it('closes its socket when the panel closes, so the server can drop the feed', () => {
    const { rerender } = render(<DepthPanel symbol="BTCUSD" onClose={() => undefined} />);
    rerender(<DepthPanel symbol={null} onClose={() => undefined} />);
    expect(FakeSocket.all[0].closed).toBe(true);
  });

  it('groups price levels (bids down, asks up) and rebuilds the cumulative size', () => {
    expect(groupLevels(depth().bids, 1, 'bid').map((l) => [l.price, l.size, l.cum])).toEqual([['100', '5', '5'], ['99', '10', '15']]);
    expect(groupLevels(depth().asks, 1, 'ask').map((l) => [l.price, l.size, l.cum])).toEqual([['101', '12', '12']]);
  });
});
