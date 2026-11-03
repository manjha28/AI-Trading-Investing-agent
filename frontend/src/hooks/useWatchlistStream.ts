import { useEffect, useRef, useState } from 'react';
import { apiRequest } from '../api';
import type { WorkspaceState } from '../components/useWorkspace';
import { watchlistCloseDisposition } from '../pages/watchlistCloseDisposition';

/** Raw quote carried on every watchlist tick (the backend QuoteResponse shape). */
export type WatchlistTickQuote = NonNullable<WorkspaceState['marketQuote']>;

export type WatchlistFeedHealth = Record<string, unknown> | null;

export type PortfolioStreamValues = {
  sequence: number;
  provider_timestamp: string;
  basis: string;
  totals: Record<string, string | null>;
  provenance: Record<string, unknown>;
  unavailable_reasons: Record<string, string | null>;
  field_metadata: Record<string, { basis: string; provider_timestamp: string | null }>;
  rows: Array<{
    instrument_id: string;
    provider_timestamp: string;
    valuation: Record<string, unknown>;
  }>;
};

export type LiveWatchlistQuote = {
  instrument_id: string;
  security_id: string | null;
  close: string;
  source: string;
  market_timestamp: string;
  market_age_seconds: number;
  freshness_status: string;
  is_synthetic: boolean;
  is_verified: boolean;
  /** Full tick quote, for consumers that need change, volume or bar detail. */
  detail: WatchlistTickQuote;
  portfolio_values?: PortfolioStreamValues;
  /** Local time the tick was received, used to judge row staleness. */
  receivedAt: number;
};

export type WatchlistStreamStatus =
  | 'idle'
  | 'connecting'
  | 'active'
  | 'reconnecting'
  | 'stale'
  | 'unavailable'
  | 'market_closed';

export type WatchlistInstrumentError = { symbol: string; reason: string; failed_at: string };

type WatchlistStreamMessage = {
  type?: string;
  status?: string;
  message?: string | null;
  last_snapshot_at?: string | null;
  feed_health?: WatchlistFeedHealth;
  instrument_id?: string;
  security_id?: string | null;
  quote?: WatchlistTickQuote;
  portfolio_values?: PortfolioStreamValues;
  symbol?: string;
  reason?: string;
  provider_timestamp?: string;
  failed_at?: string;
};

export type WatchlistStreamOptions = {
  /** Watchlist to stream. undefined uses the default watchlist; null disables the stream. */
  watchlistId?: string | null;
  enabled?: boolean;
  /** Reconnect if no tick arrives within this many ms of opening the socket. */
  firstTickTimeoutMs?: number;
  /** Delay before reconnecting after the market closes. Defaults to the next NSE open. */
  marketClosedRetryMs?: () => number;
  onTick?: () => void;
  onError?: (message: string) => void;
};

const RECONNECT_INITIAL_MS = 1000;
const RECONNECT_MAX_MS = 60_000;
const FLUSH_INTERVAL_MS = Math.max(
  16,
  Number(import.meta.env.VITE_WATCHLIST_RENDER_INTERVAL_MS) || 16,
);
const HEARTBEAT_MS = 15_000;
const AUTH_CLOSE_CODES = [4401, 4403, 4404];

export function nextNseOpenDelayMs(): number {
  const now = new Date();
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Kolkata',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).formatToParts(now);
  const part = (type: string) => parts.find((item) => item.type === type)?.value ?? '';
  let nextOpen = Date.UTC(
    Number(part('year')),
    Number(part('month')) - 1,
    Number(part('day')),
    3,
    45,
  );
  if (nextOpen <= now.getTime()) {
    const tomorrow = new Date(nextOpen);
    tomorrow.setUTCDate(tomorrow.getUTCDate() + 1);
    nextOpen = tomorrow.getTime();
  }
  while ([0, 6].includes(new Date(nextOpen).getUTCDay())) {
    const nextWeekday = new Date(nextOpen);
    nextWeekday.setUTCDate(nextWeekday.getUTCDate() + 1);
    nextOpen = nextWeekday.getTime();
  }
  return Math.max(1000, nextOpen - now.getTime());
}

/**
 * Owns the only watchlist WebSocket connection in the app. Ticks are batched into
 * one state update per FLUSH_INTERVAL_MS, and the newest quote per instrument wins:
 * a tick older than the value already held is dropped.
 */
export function useWatchlistStream({
  watchlistId,
  enabled = true,
  firstTickTimeoutMs,
  marketClosedRetryMs,
  onTick,
  onError,
}: WatchlistStreamOptions = {}) {
  const [status, setStatus] = useState<WatchlistStreamStatus>('idle');
  const [message, setMessage] = useState<string | null>(null);
  const [quotes, setQuotes] = useState<Record<string, LiveWatchlistQuote>>({});
  const [feedHealth, setFeedHealth] = useState<WatchlistFeedHealth>(null);
  const [portfolioValues, setPortfolioValues] = useState<PortfolioStreamValues | null>(null);
  const [instrumentErrors, setInstrumentErrors] = useState<Record<string, WatchlistInstrumentError>>({});
  const [lastSnapshotAt, setLastSnapshotAt] = useState<string | null>(null);
  const callbacksRef = useRef({ onTick, onError, marketClosedRetryMs });

  useEffect(() => {
    callbacksRef.current = { onTick, onError, marketClosedRetryMs };
  }, [onTick, onError, marketClosedRetryMs]);

  const active = enabled && watchlistId !== null;

  useEffect(() => {
    if (!active) {
      setStatus('idle');
      setQuotes((current) => (Object.keys(current).length ? {} : current));
      setPortfolioValues(null);
      setInstrumentErrors({});
      setMessage(null);
      return;
    }
    const requestedWatchlistId: string | undefined = watchlistId ?? undefined;

    let cancelled = false;
    let socket: WebSocket | null = null;
    let reconnectTimer: number | undefined;
    let flushTimer: number | undefined;
    let firstTickTimer: number | undefined;
    let heartbeatTimer: number | undefined;
    let reconnectDelayMs = RECONNECT_INITIAL_MS;
    let lastStatus: WatchlistStreamStatus = 'connecting';
    let unavailableReceived = false;
    let receivedTick = false;
    let lastPacketReceivedAt: number | null = null;
    let socketOpenedAt: number | null = null;
    let staleAfterSeconds = 30;
    let committed: Record<string, LiveWatchlistQuote> = {};
    const pending = new Map<string, LiveWatchlistQuote>();
    let pendingFeedHealth: WatchlistFeedHealth | undefined;

    setStatus('connecting');
    setMessage(null);
    setQuotes((current) => (Object.keys(current).length ? {} : current));
    setFeedHealth(null);
    setPortfolioValues(null);
    setLastSnapshotAt(null);

    const flush = () => {
      flushTimer = undefined;
      if (cancelled) return;
      if (pending.size > 0) {
        committed = { ...committed, ...Object.fromEntries(pending) };
        pending.clear();
        setQuotes(committed);
        callbacksRef.current.onTick?.();
      }
      if (pendingFeedHealth !== undefined) {
        setFeedHealth(pendingFeedHealth);
        pendingFeedHealth = undefined;
      }
    };

    const scheduleFlush = () => {
      if (flushTimer === undefined) flushTimer = window.setTimeout(flush, FLUSH_INTERVAL_MS);
    };

    const staleTimer = window.setInterval(() => {
      if (
        (lastStatus === 'active' || lastStatus === 'connecting')
        && (lastPacketReceivedAt ?? socketOpenedAt) !== null
        && Number.isFinite(staleAfterSeconds)
        && Date.now() - (lastPacketReceivedAt ?? socketOpenedAt ?? Date.now()) > staleAfterSeconds * 1000
      ) {
        lastStatus = 'stale';
        setStatus('stale');
        setMessage(`No market packet received for ${staleAfterSeconds} seconds.`);
      }
    }, 1000);

    const clearSocketTimers = () => {
      window.clearTimeout(firstTickTimer);
      firstTickTimer = undefined;
      window.clearInterval(heartbeatTimer);
      heartbeatTimer = undefined;
    };

    const scheduleReconnect = (delayMs: number) => {
      if (cancelled || reconnectTimer !== undefined) return;
      reconnectTimer = window.setTimeout(() => {
        reconnectTimer = undefined;
        void connect();
      }, delayMs);
    };

    const scheduleBackoff = () => {
      scheduleReconnect(reconnectDelayMs);
      reconnectDelayMs = Math.min(RECONNECT_MAX_MS, reconnectDelayMs * 2);
    };

    const connect = async () => {
      if (cancelled) return;
      try {
        let resolvedId = requestedWatchlistId;
        if (resolvedId === undefined) {
          const watchlists = await apiRequest<{ id: string; is_default: boolean }[]>('/market-data/watchlists');
          if (cancelled) return;
          const watchlist = watchlists.find((item) => item.is_default) ?? watchlists[0];
          if (!watchlist) {
            setStatus('unavailable');
            setMessage('No watchlist is configured.');
            return;
          }
          resolvedId = watchlist.id;
        }
        const url = new URL(`/api/v1/market-data/watchlists/${resolvedId}/stream`, window.location.href);
        url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
        const ws = new WebSocket(url);
        socket = ws;
        receivedTick = false;
        unavailableReceived = false;

        ws.onopen = () => {
          socketOpenedAt = Date.now();
          if (firstTickTimeoutMs) {
            firstTickTimer = window.setTimeout(() => {
              firstTickTimer = undefined;
              ws.close(4008, 'No tick received');
            }, firstTickTimeoutMs);
          }
          heartbeatTimer = window.setInterval(() => {
            if (ws.readyState === WebSocket.OPEN) ws.send('ping');
          }, HEARTBEAT_MS);
        };

        ws.onmessage = (event) => {
          let parsed: WatchlistStreamMessage;
          try {
            parsed = JSON.parse(String(event.data)) as WatchlistStreamMessage;
          } catch {
            setStatus('unavailable');
            return;
          }
          const configuredStaleAfter = Number(
            (parsed.feed_health as { stale_after_seconds?: unknown } | null)?.stale_after_seconds,
          );
          if (Number.isFinite(configuredStaleAfter) && configuredStaleAfter >= 1) {
            staleAfterSeconds = configuredStaleAfter;
          }
          if (parsed.type === 'status' && parsed.status) {
            const next = parsed.status as WatchlistStreamStatus;
            lastStatus = next;
            if (next === 'unavailable') unavailableReceived = true;
            setStatus(next);
            setMessage(parsed.message ?? null);
            if (parsed.feed_health !== undefined) setFeedHealth(parsed.feed_health);
            if (
              (parsed.feed_health as { last_successful_packet_at?: unknown } | null)?.last_successful_packet_at
            ) lastPacketReceivedAt = Date.now();
            if (parsed.last_snapshot_at !== undefined) setLastSnapshotAt(parsed.last_snapshot_at);
            return;
          }
          if (parsed.type === 'portfolio_values' && parsed.portfolio_values) {
            lastPacketReceivedAt = Date.now();
            if (lastStatus !== 'active') {
              lastStatus = 'active';
              setStatus('active');
              setMessage(null);
            }
            setPortfolioValues((current) => (
              !current || parsed.portfolio_values!.sequence > current.sequence
                ? parsed.portfolio_values!
                : current
            ));
            if (parsed.feed_health !== undefined) setFeedHealth(parsed.feed_health);
            return;
          }
          if (parsed.type === 'instrument_error' && parsed.instrument_id && parsed.symbol && parsed.reason) {
            pending.delete(parsed.instrument_id);
            delete committed[parsed.instrument_id];
            if (flushTimer !== undefined && pending.size === 0) {
              window.clearTimeout(flushTimer);
              flushTimer = undefined;
            }
            const failedAt = parsed.failed_at ?? new Date().toISOString();
            setInstrumentErrors((current) => ({
              ...current,
              [parsed.instrument_id!]: {
                symbol: parsed.symbol!,
                reason: parsed.reason!,
                failed_at: failedAt,
              },
            }));
            setQuotes((current) => {
              if (!current[parsed.instrument_id!]) return current;
              const next = { ...current };
              delete next[parsed.instrument_id!];
              return next;
            });
            setMessage(`${parsed.symbol} unavailable: ${parsed.reason}`);
            if (parsed.feed_health !== undefined) setFeedHealth(parsed.feed_health);
            return;
          }
          if (parsed.type !== 'tick') return;
          lastPacketReceivedAt = Date.now();
          const instrumentId = parsed.instrument_id;
          const quote = parsed.quote;
          if (
            typeof instrumentId !== 'string'
            || typeof quote?.bar?.close !== 'string'
            || typeof quote.market_timestamp !== 'string'
          ) {
            return;
          }
          if (!receivedTick) {
            receivedTick = true;
            window.clearTimeout(firstTickTimer);
            firstTickTimer = undefined;
            reconnectDelayMs = RECONNECT_INITIAL_MS;
          }
          if (lastStatus !== 'active') {
            lastStatus = 'active';
            setStatus('active');
            setMessage(null);
          }
          const current = pending.get(instrumentId) ?? committed[instrumentId];
          if (current && Date.parse(quote.market_timestamp) < Date.parse(current.market_timestamp)) return;
          pending.set(instrumentId, {
            instrument_id: instrumentId,
            security_id: parsed.security_id ?? null,
            close: quote.bar.close,
            source: quote.source,
            market_timestamp: quote.market_timestamp,
            market_age_seconds: quote.market_age_seconds,
            freshness_status: quote.freshness_status,
            is_synthetic: quote.is_synthetic,
            is_verified: quote.is_verified,
            detail: quote,
            portfolio_values: parsed.portfolio_values ?? current?.portfolio_values,
            receivedAt: Date.now(),
          });
          setInstrumentErrors((current) => {
            if (!current[instrumentId]) return current;
            const next = { ...current };
            delete next[instrumentId];
            return next;
          });
          if (parsed.feed_health !== undefined) pendingFeedHealth = parsed.feed_health;
          if (parsed.portfolio_values) {
            setPortfolioValues((current) => (
              !current || parsed.portfolio_values!.sequence > current.sequence
                ? parsed.portfolio_values!
                : current
            ));
          }
          scheduleFlush();
        };

        ws.onclose = (event) => {
          socket = null;
          clearSocketTimers();
          if (cancelled) return;
          if (AUTH_CLOSE_CODES.includes(event.code)) {
            setStatus('unavailable');
            setMessage('Live feed access was refused.');
            return;
          }
          const disposition = watchlistCloseDisposition(
            event.code,
            event.reason,
            lastStatus === 'market_closed',
            unavailableReceived,
          );
          if (disposition === 'market_closed') {
            setStatus('market_closed');
            scheduleReconnect(callbacksRef.current.marketClosedRetryMs?.() ?? nextNseOpenDelayMs());
          } else if (disposition === 'clean') {
            setStatus('reconnecting');
            scheduleBackoff();
          } else {
            setStatus(disposition === 'unavailable' ? 'unavailable' : 'reconnecting');
            scheduleBackoff();
          }
        };
      } catch (error) {
        if (cancelled) return;
        const text = error instanceof Error ? error.message : 'Live Dhan feed is unavailable.';
        setStatus('unavailable');
        setMessage(text);
        callbacksRef.current.onError?.(text);
        scheduleBackoff();
      }
    };

    void connect();
    return () => {
      cancelled = true;
      window.clearTimeout(reconnectTimer);
      window.clearTimeout(flushTimer);
      window.clearInterval(staleTimer);
      clearSocketTimers();
      socket?.close();
      socket = null;
    };
  }, [active, watchlistId, firstTickTimeoutMs]);

  return { status, message, quotes, feedHealth, portfolioValues, instrumentErrors, lastSnapshotAt, setStatus };
}
