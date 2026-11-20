import { useEffect, useState } from 'react';

export type CommodityTick = {
  key: string;
  symbol: string;
  ltp: string;
  previous_close: string | null;
  change: string | null;
  change_pct: string | null;
  open: string;
  high: string;
  low: string;
  volume: string;
  /** Server clock at the moment the tick was relayed (the feed's own tick time is not relied on). */
  received_at: string;
  /** Local time the browser received it, for flash/age display. */
  receivedAt: number;
};

export type CommodityStreamStatus = 'idle' | 'connecting' | 'active' | 'reconnecting' | 'market_closed' | 'unavailable' | 'stale';

type Message = {
  type?: string;
  status?: string;
  message?: string | null;
} & Partial<Omit<CommodityTick, 'receivedAt'>>;

const RECONNECT_INITIAL_MS = 1000;
const RECONNECT_MAX_MS = 30_000;
const FLUSH_MS = 150;
const HEARTBEAT_MS = 15_000;
const AUTH_CLOSE_CODES = [4401, 4403, 4404];

/** Live MCX commodity ticks. One socket; newest tick per contract wins; updates are batched. */
export function useCommodityStream(enabled = true) {
  const [status, setStatus] = useState<CommodityStreamStatus>('idle');
  const [message, setMessage] = useState<string | null>(null);
  const [ticks, setTicks] = useState<Record<string, CommodityTick>>({});

  useEffect(() => {
    if (!enabled) { setStatus('idle'); return undefined; }
    let cancelled = false;
    let socket: WebSocket | undefined;
    let retryTimer: number | undefined;
    let heartbeat: number | undefined;
    let flushTimer: number | undefined;
    let delay = RECONNECT_INITIAL_MS;
    const pending = new Map<string, CommodityTick>();

    const flush = () => {
      flushTimer = undefined;
      if (pending.size === 0) return;
      const batch = new Map(pending);
      pending.clear();
      setTicks((current) => {
        const next = { ...current };
        batch.forEach((tick, key) => { next[key] = tick; });
        return next;
      });
    };

    const connect = () => {
      if (cancelled) return;
      setStatus((current) => (current === 'active' || current === 'market_closed' ? 'reconnecting' : 'connecting'));
      const url = new URL('/api/v1/market-data/commodities/stream', window.location.href);
      url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
      const ws = new WebSocket(url);
      socket = ws;
      ws.onopen = () => {
        delay = RECONNECT_INITIAL_MS;
        heartbeat = window.setInterval(() => { if (ws.readyState === WebSocket.OPEN) ws.send('ping'); }, HEARTBEAT_MS);
      };
      ws.onmessage = (event) => {
        let parsed: Message;
        try { parsed = JSON.parse(String(event.data)) as Message; } catch { return; }
        if (parsed.type === 'status' && parsed.status) {
          const next = parsed.status as CommodityStreamStatus;
          if (['connecting', 'active', 'reconnecting', 'market_closed', 'unavailable', 'idle'].includes(next)) {
            // a packet-bearing "active" must not hide a closed-market notice from the server
            setStatus((current) => (next === 'active' && current === 'market_closed' ? current : next));
          }
          setMessage(parsed.message ?? null);
          return;
        }
        if (parsed.type === 'tick' && parsed.key && parsed.ltp !== undefined) {
          const tick = { ...parsed, receivedAt: Date.now() } as CommodityTick;
          pending.set(tick.key, tick);
          setStatus((current) => (current === 'market_closed' || current === 'active' ? current : 'active'));
          if (flushTimer === undefined) flushTimer = window.setTimeout(flush, FLUSH_MS);
        }
      };
      ws.onclose = (event) => {
        window.clearInterval(heartbeat);
        if (cancelled) return;
        if (AUTH_CLOSE_CODES.includes(event.code)) {
          setStatus('unavailable');
          setMessage((current) => current ?? 'The live commodity feed needs a verified Dhan session.');
          return; // the reason will not change by retrying
        }
        setStatus('reconnecting');
        retryTimer = window.setTimeout(connect, delay);
        delay = Math.min(RECONNECT_MAX_MS, delay * 2);
      };
      ws.onerror = () => ws.close();
    };

    connect();
    return () => {
      cancelled = true;
      window.clearTimeout(retryTimer);
      window.clearTimeout(flushTimer);
      window.clearInterval(heartbeat);
      socket?.close();
    };
  }, [enabled]);

  return { status, message, ticks };
}
