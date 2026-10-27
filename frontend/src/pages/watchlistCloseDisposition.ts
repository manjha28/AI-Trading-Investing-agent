export type WatchlistCloseDisposition =
  | 'market_closed'
  | 'unavailable'
  | 'clean'
  | 'reconnect';

export function watchlistCloseDisposition(
  code: number,
  reason: string,
  marketClosed: boolean,
  unavailableStatusReceived: boolean,
): WatchlistCloseDisposition {
  if (
    code === 1000
    && (marketClosed || reason.trim().toLowerCase() === 'market closed')
  ) {
    return 'market_closed';
  }
  if (code === 1000 && unavailableStatusReceived) return 'unavailable';
  if (code === 1000) return 'clean';
  if ([4401, 4403, 4404].includes(code)) return 'unavailable';
  return 'reconnect';
}
