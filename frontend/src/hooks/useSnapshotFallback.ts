import { useEffect, useState } from 'react';
import { ApiRequestError, apiRequest } from '../api';

export type SnapshotFreshness = 'fresh' | 'recent' | 'stale' | 'unavailable' | string;

export type SnapshotBar = {
  instrument_id: string;
  provider: string;
  interval: string;
  bar_timestamp: string;
  open: string;
  high: string;
  low: string;
  close: string;
  volume: string;
  ingested_at: string;
  age_seconds: number;
  freshness: SnapshotFreshness;
  is_synthetic: boolean;
};

export type SnapshotStatus = 'idle' | 'loading' | 'ready' | 'unavailable' | 'error';

type LatestResponse = {
  bars: SnapshotBar[];
  missing_instrument_ids: string[];
};

export const SNAPSHOT_REFRESH_MS = 30_000;
export const SNAPSHOT_BACKOFF_MAX_MS = 300_000;

/** Delay before the next attempt after `failures` consecutive failed loads. */
export function snapshotRetryDelayMs(failures: number, baseMs: number = SNAPSHOT_REFRESH_MS): number {
  return Math.min(SNAPSHOT_BACKOFF_MAX_MS, baseMs * 2 ** Math.max(0, failures - 1));
}

/**
 * Chooses which observation a view should display. A cached snapshot may only
 * replace a live value when it is strictly newer, so a delayed snapshot can
 * never overwrite fresher WebSocket data.
 */
export function selectFreshestSource(
  live: { observedAt: string } | null | undefined,
  snapshot: { observedAt: string } | null | undefined,
): 'live' | 'snapshot' | 'none' {
  if (live) {
    if (!snapshot) return 'live';
    return Date.parse(snapshot.observedAt) > Date.parse(live.observedAt) ? 'snapshot' : 'live';
  }
  return snapshot ? 'snapshot' : 'none';
}

/**
 * Loads the last persisted/cached bar for each instrument from the Valkey
 * snapshot route. Runs only while `enabled` (the live stream is down), and
 * refreshes on a slow interval for as long as it stays enabled. Nothing is
 * requested during healthy WebSocket operation.
 */
export function useSnapshotFallback({
  instrumentIds,
  enabled,
  refreshMs = SNAPSHOT_REFRESH_MS,
}: {
  instrumentIds: string[];
  enabled: boolean;
  refreshMs?: number;
}) {
  const [snapshots, setSnapshots] = useState<Record<string, SnapshotBar>>({});
  const [status, setStatus] = useState<SnapshotStatus>('idle');
  const [message, setMessage] = useState('');
  const [fetchedAt, setFetchedAt] = useState<number | null>(null);
  const idsKey = [...new Set(instrumentIds)].sort().join(',');

  useEffect(() => {
    if (!enabled || !idsKey) {
      setSnapshots({});
      setStatus('idle');
      setMessage('');
      setFetchedAt(null);
      return;
    }
    let cancelled = false;
    let timer: number | undefined;
    let failures = 0;

    const load = async () => {
      setStatus((current) => (current === 'ready' ? current : 'loading'));
      let retryMs: number | null = refreshMs;
      try {
        const response = await apiRequest<LatestResponse>(
          `/market-stream/latest?instrument_ids=${encodeURIComponent(idsKey)}`,
        );
        if (cancelled) return;
        failures = 0;
        setSnapshots(Object.fromEntries(response.bars.map((bar) => [bar.instrument_id, bar])));
        setStatus('ready');
        setMessage(
          response.missing_instrument_ids.length
            ? `No stored snapshot for ${response.missing_instrument_ids.length} instrument(s).`
            : '',
        );
        setFetchedAt(Date.now());
      } catch (error) {
        if (cancelled) return;
        setSnapshots({});
        if (error instanceof ApiRequestError && error.code === 'market_stream_disabled') {
          // Disabled is a configuration state: polling cannot succeed until the server restarts.
          retryMs = null;
          setStatus('unavailable');
          setMessage('Market stream is disabled on the server. Snapshot polling is paused.');
        } else if (error instanceof ApiRequestError && error.status === 503) {
          failures += 1;
          retryMs = snapshotRetryDelayMs(failures, refreshMs);
          setStatus('unavailable');
          setMessage('Cached snapshot store is unreachable. Retrying with backoff.');
        } else {
          failures += 1;
          retryMs = snapshotRetryDelayMs(failures, refreshMs);
          setStatus('error');
          setMessage(error instanceof Error ? error.message : 'Cached snapshot could not be loaded.');
        }
      } finally {
        if (!cancelled && retryMs !== null) timer = window.setTimeout(() => void load(), retryMs);
      }
    };

    void load();
    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [enabled, idsKey, refreshMs]);

  return { snapshots, status, message, fetchedAt };
}
