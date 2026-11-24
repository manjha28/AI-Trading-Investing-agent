import { useEffect, useMemo, useRef, useState } from 'react';
import { Sheet } from '../components/ui/Sheet';

export type Level = { price: string; size: string; cum: string };
export type Depth = {
  type: 'depth'; symbol: string; stale: boolean; age_seconds: number | null; provisional: boolean; gaps: number; resyncs: number; bids: Level[]; asks: Level[];
  metrics: { best_bid: string | null; best_ask: string | null; mid: string | null; spread: string | null; spread_bps: string | null; imbalance_5: string | null; imbalance_20: string | null };
  trades: { price: string; size: string; aggressor: 'buy' | 'sell'; time: string }[];
  mark: { mark_price: string | null; index_price: string | null; funding_rate: string | null; open_interest_usd: string | null };
  funding_countdown_seconds: number; funding_schedule: string; disclaimer: string;
};
/** A panel with no update for this long shows STALE (the server also reports its own book age). */
export const STALE_AFTER_MS = 3_000;
const RECONNECT_MS = [1_000, 2_000, 5_000, 15_000, 30_000];

/** One websocket per open panel; the server shares its Delta connection between viewers. Closing the panel closes the socket, which is what lets the server drop the feed. */
export function useDepthStream(symbol: string | null) {
  const [depth, setDepth] = useState<Depth | null>(null);
  const [lastAt, setLastAt] = useState<number | null>(null);
  const [state, setState] = useState<'idle' | 'connecting' | 'open' | 'unavailable'>('idle');
  const [message, setMessage] = useState<string | null>(null);
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!symbol) { setState('idle'); return undefined; }
    let cancelled = false; let ws: WebSocket | undefined; let retry: number | undefined; let attempt = 0;
    setDepth(null); setLastAt(null);
    const connect = () => {
      if (cancelled) return;
      setState('connecting');
      const url = new URL(`/api/v1/delta-depth/${encodeURIComponent(symbol)}/stream`, window.location.href);
      url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
      ws = new WebSocket(url);
      ws.onopen = () => { attempt = 0; setState('open'); };
      ws.onmessage = (e) => {
        let m: { type?: string; status?: string; message?: string } & Partial<Depth>;
        try { m = JSON.parse(String(e.data)); } catch { return; }
        if (m.type === 'depth') { setDepth(m as Depth); setLastAt(Date.now()); setMessage(null); }
        else if (m.type === 'status') { setMessage(m.message ?? null); if (m.status === 'unavailable') setState('unavailable'); }
      };
      ws.onclose = (e) => { if (cancelled || [4401, 4403, 1008].includes(e.code)) return; setState('connecting'); retry = window.setTimeout(connect, RECONNECT_MS[Math.min(attempt++, RECONNECT_MS.length - 1)]); };
    };
    connect();
    const clock = window.setInterval(() => setNow(Date.now()), 1_000);
    return () => { cancelled = true; window.clearTimeout(retry); window.clearInterval(clock); ws?.close(); };   // unsubscribes on close
  }, [symbol]);
  const stale = depth === null || lastAt === null || now - lastAt > STALE_AFTER_MS || depth.stale;
  return { depth, state, message, stale, ageMs: lastAt === null ? null : now - lastAt };
}

const n = (v: string | null | undefined, d = 2) => (v == null || v === '' || !Number.isFinite(Number(v)) ? '—' : Number(v).toLocaleString('en-US', { maximumFractionDigits: d }));
const bar = (cum: string, max: number) => ({ width: `${max > 0 ? Math.min(100, (Number(cum) / max) * 100) : 0}%` });

/** Group levels into buckets of `step` (bids round down, asks up) and rebuild the cumulative size, client side. */
export function groupLevels(rows: Level[], step: number, side: 'bid' | 'ask'): Level[] {
  if (!(step > 0)) return rows;
  const m = new Map<number, number>();
  for (const r of rows) {
    const bucket = (side === 'bid' ? Math.floor(Number(r.price) / step) : Math.ceil(Number(r.price) / step)) * step;
    m.set(bucket, (m.get(bucket) ?? 0) + Number(r.size));
  }
  const ordered = [...m.entries()].sort((a, b) => (side === 'bid' ? b[0] - a[0] : a[0] - b[0]));
  let total = 0;
  return ordered.map(([p, s]) => { total += s; return { price: String(Number(p.toFixed(8))), size: String(s), cum: String(total) }; });
}

function Book({ depth, group }: { depth: Depth; group: number }) {
  const bids = useMemo(() => groupLevels(depth.bids, group, 'bid').slice(0, 20), [depth.bids, group]);
  const asks = useMemo(() => groupLevels(depth.asks, group, 'ask').slice(0, 20), [depth.asks, group]);
  const max = Math.max(Number(bids[bids.length - 1]?.cum ?? 0), Number(asks[asks.length - 1]?.cum ?? 0));
  const row = (l: Level, side: 'bid' | 'ask') => (
    <li key={`${side}${l.price}`} className={`depth-row depth-${side}`}><i style={bar(l.cum, max)} /><span>{n(l.price, 4)}</span><span>{n(l.size, 0)}</span><span>{n(l.cum, 0)}</span></li>
  );
  return (
    <div className="depth-book">
      <div className="depth-head"><span>Price</span><span>Size</span><span>Total</span></div>
      <ul className="depth-asks">{[...asks].reverse().map((l) => row(l, 'ask'))}</ul>
      <p className="depth-mid"><b>{n(depth.metrics.mid, 4)}</b> mid · spread {n(depth.metrics.spread, 4)} ({n(depth.metrics.spread_bps, 2)} bps)</p>
      <ul className="depth-bids">{bids.map((l) => row(l, 'bid'))}</ul>
    </div>
  );
}

const countdown = (s: number) => `${Math.floor(s / 3600)}h ${String(Math.floor((s % 3600) / 60)).padStart(2, '0')}m ${String(s % 60).padStart(2, '0')}s`;

/** Side panel for one perpetual: order book, trades tape, mark / index / funding / open interest. Display only; no order is placed or implied. */
export function DepthPanel({ symbol, onClose }: { symbol: string | null; onClose: () => void }) {
  const { depth, state, message, stale } = useDepthStream(symbol);
  const [group, setGroup] = useState(0);
  const steps = useMemo(() => {
    const spread = Number(depth?.metrics.spread ?? 0);
    const mid = Number(depth?.metrics.mid ?? 0);
    if (!(mid > 0)) return [] as number[];
    const base = spread > 0 ? spread : mid * 0.0001;
    return [0, base * 2, base * 10, base * 50].map((x) => (x === 0 ? 0 : Number(x.toPrecision(1))));
  }, [depth?.metrics.spread, depth?.metrics.mid]);
  const remaining = useRef(0);
  remaining.current = depth?.funding_countdown_seconds ?? 0;
  const imb = (v: string | null) => (v == null ? '—' : `${Number(v) > 0 ? '+' : ''}${(Number(v) * 100).toFixed(1)}% ${Number(v) > 0 ? 'bid-heavy' : Number(v) < 0 ? 'ask-heavy' : ''}`);
  return (
    <Sheet open={symbol !== null} onClose={onClose} title={symbol ? `${symbol} depth` : 'Depth'}>
      {symbol && (
        <div className="depth-panel">
          <p className="depth-status"><span className={`depth-badge ${stale ? 'stale' : 'live'}`} role="status">{stale ? (depth ? 'STALE' : state === 'unavailable' ? 'UNAVAILABLE' : 'CONNECTING') : 'LIVE'}</span>
            {depth?.provisional && <span className="depth-badge stale" title="Rebuilt from the REST snapshot while the live feed resynchronises">RESYNCING</span>}
            <label>Group <select aria-label="Price grouping" value={group} onChange={(e) => setGroup(Number(e.target.value))}>{(steps.length ? steps : [0]).map((s) => <option key={s} value={s}>{s === 0 ? 'None' : String(s)}</option>)}</select></label></p>
          {message && <p className="alerts-notice" role="alert">{message}</p>}
          {!depth ? <p className="crypto-sub">{state === 'unavailable' ? 'Depth is not available for this session.' : 'Waiting for the first book from Delta…'}</p> : (
            <>
              <dl className="depth-stats">
                <div><dt>Mark</dt><dd>{n(depth.mark.mark_price, 4)}</dd></div><div><dt>Index</dt><dd>{n(depth.mark.index_price, 4)}</dd></div>
                <div><dt>Funding</dt><dd>{depth.mark.funding_rate == null ? '—' : `${n(depth.mark.funding_rate, 4)}%`}<small title={depth.funding_schedule}> in {countdown(depth.funding_countdown_seconds)}</small></dd></div>
                <div><dt>Open interest</dt><dd>{depth.mark.open_interest_usd == null ? '—' : `$${n(depth.mark.open_interest_usd, 0)}`}</dd></div>
                <div><dt>Imbalance top 5</dt><dd>{imb(depth.metrics.imbalance_5)}</dd></div><div><dt>Imbalance top 20</dt><dd>{imb(depth.metrics.imbalance_20)}</dd></div>
              </dl>
              <Book depth={depth} group={group} />
              <h3>Recent trades</h3>
              <ul className="depth-tape">{depth.trades.slice(0, 50).map((t, i) => <li key={`${t.time}-${i}`} className={t.aggressor === 'buy' ? 'positive' : 'negative'}><span>{n(t.price, 4)}</span><span>{n(t.size, 0)}</span><span>{t.aggressor === 'buy' ? 'buy' : 'sell'}</span></li>)}</ul>
              <p className="crypto-sub">{depth.disclaimer} Funding countdown assumes Delta&apos;s 8-hourly schedule.</p>
            </>
          )}
        </div>
      )}
    </Sheet>
  );
}
