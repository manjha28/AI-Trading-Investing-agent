import { memo, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { apiRequest } from '../api';
import { DepthPanel } from './DepthPanel';
import { FeedToggle } from './FeedToggle';
import { messageOf, type Market } from './cryptoApi';

type Mode = 'all' | 'watchlist' | 'gainers' | 'losers';
type SortKey = 'symbol' | 'last' | 'chg' | 'chgPct' | 'volume' | 'oi' | 'funding';
const POLL_MS = 1_000;
/** After a failed poll wait 2 s, 5 s, 15 s, then 60 s (capped); one success returns to normal. */
export const BACKOFF_MS = [2_000, 5_000, 15_000, 60_000];
const istTime = (d: Date) => d.toLocaleTimeString('en-IN', { timeZone: 'Asia/Kolkata', hour: '2-digit', minute: '2-digit', hour12: false });
const STALE_AFTER_S = 90;

export const num = (value: string | null | undefined): number | null => { if (value === null || value === undefined || value === '') return null; const n = Number(value); return Number.isFinite(n) ? n : null; };
const price = (value: string | null | undefined): string => { const n = num(value); return n === null ? '—' : n.toLocaleString('en-US', { maximumFractionDigits: n >= 100 ? 2 : n >= 1 ? 4 : 8 }); };
const compact = (value: number | null): string => (value === null ? '—' : value >= 1e9 ? `${(value / 1e9).toFixed(2)}B` : value >= 1e6 ? `${(value / 1e6).toFixed(2)}M` : value >= 1e3 ? `${(value / 1e3).toFixed(1)}K` : value.toFixed(2));
const signed = (value: number | null, suffix = '', digits = 2): string => (value === null ? '—' : `${value > 0 ? '▲ +' : value < 0 ? '▼ ' : ''}${value.toFixed(digits)}${suffix}`);
const tone = (value: number | null): string => (value === null || value === 0 ? '' : value > 0 ? 'positive' : 'negative');

function DayRange({ low, high, last }: { low: number | null; high: number | null; last: number | null }) {
  if (low === null || high === null || high <= low) return <>—</>;
  const pos = last === null ? null : Math.min(100, Math.max(0, ((last - low) / (high - low)) * 100));
  return <><span>{price(String(low))}</span><span className="watch-range-bar">{pos !== null && <i style={{ left: `${pos}%` }} />}</span><span>{price(String(high))}</span></>;
}

type ColId = 'chg' | 'range' | 'oi' | 'oi6' | 'mark' | 'bidask' | 'updated';
const EXTRA_COLS: { id: ColId; label: string }[] = [{ id: 'chg', label: 'Change (abs)' }, { id: 'range', label: '24h range' }, { id: 'oi', label: 'Open interest' }, { id: 'oi6', label: 'OI 6h' }, { id: 'mark', label: 'Mark' }, { id: 'bidask', label: 'Bid / Ask' }, { id: 'updated', label: 'Updated' }];
type RowProps = { m: Market; selected: boolean; starred: boolean; live: boolean; cols: ReadonlySet<ColId>; onSelect: (m: Market) => void; onStar: (symbol: string) => void };

const Row = memo(function Row({ m, selected, starred, live, cols, onSelect, onStar }: RowProps) {
  const t = m.ticker;
  const last = num(m.last_price);
  const prev = useRef<number | null>(last);
  const [flash, setFlash] = useState<'up' | 'down' | ''>('');
  useEffect(() => {
    if (last !== null && prev.current !== null && last !== prev.current) {
      setFlash(last > prev.current ? 'up' : 'down');
      const timer = window.setTimeout(() => setFlash(''), 700);
      prev.current = last;
      return () => window.clearTimeout(timer);
    }
    prev.current = last;
    return undefined;
  }, [last]);
  const open = num(t?.open);
  const chg = last !== null && open !== null ? last - open : null;
  const pct = num(t?.ltp_change_24h);
  const funding = num(t?.funding_rate);
  const oiChange = num(t?.oi_change_usd_6h);
  const age = m.quote_time ? Math.max(0, Math.round((Date.now() - new Date(m.quote_time).getTime()) / 1000)) : null;
  return (
    <tr className={`${selected ? 'watch-selected' : ''} ${last === null ? 'watch-nodata' : ''}`} onClick={() => onSelect(m)} aria-selected={selected}>
      <td className="watch-starcell"><button type="button" className={`watch-star ${starred ? 'on' : ''}`} aria-label={`${starred ? 'Remove' : 'Add'} ${m.symbol} ${starred ? 'from' : 'to'} watchlist`} aria-pressed={starred} onClick={(e) => { e.stopPropagation(); onStar(m.symbol); }}>{starred ? '★' : '☆'}</button></td>
      <td className="watch-symbol" title={`${t?.underlying_asset_symbol ?? m.symbol}${t?.leverage ? ` · up to ${t.leverage}x` : ''}`}><span className={`watch-dot ${live && last !== null ? 'live' : ''}`} /><button type="button" className="link-button" onClick={() => onSelect(m)}><strong>{m.symbol}</strong></button></td>
      <td className={`watch-num watch-ltp ${flash ? `flash-${flash}` : ''}`}>{price(m.last_price)}</td>
      {cols.has('chg') && <td className={`watch-num ${tone(chg)}`}>{signed(chg, '', chg !== null && Math.abs(chg) < 1 ? 4 : 2)}</td>}
      <td className={`watch-num ${tone(pct)}`}>{signed(pct, '%')}</td>
      {cols.has('range') && <td className="watch-range" aria-label="24h range"><DayRange low={num(t?.low)} high={num(t?.high)} last={last} /></td>}
      <td className="watch-num">{compact(num(t?.turnover_usd ?? m.volume_24h))}</td>
      {cols.has('oi') && <td className="watch-num">{compact(num(t?.oi_value_usd))}</td>}
      {cols.has('oi6') && <td className={`watch-num ${tone(oiChange)}`}>{oiChange === null ? '—' : `${oiChange > 0 ? '▲ +' : oiChange < 0 ? '▼ -' : ''}${compact(Math.abs(oiChange))}`}</td>}
      <td className={`watch-num ${tone(funding)}`} title="Funding rate as reported by Delta">{funding === null ? '—' : `${funding.toFixed(4)}%`}</td>
      {cols.has('mark') && <td className="watch-num">{price(m.mark_price)}</td>}
      {cols.has('bidask') && <td className="watch-num">{t?.best_bid && t?.best_ask ? `${price(t.best_bid)} / ${price(t.best_ask)}` : '—'}</td>}
      {cols.has('updated') && <td className={`watch-num watch-age ${age !== null && age > STALE_AFTER_S ? 'watch-stale' : ''}`} title={`Source: ${m.quote_source ?? 'unknown'}${m.quote_time ? ` · observed ${m.quote_time}` : ''}`}>{age === null ? '—' : age < 60 ? `${age}s` : `${Math.round(age / 60)}m`}</td>}
    </tr>
  );
});

/** The Crypto watch: the same strip, toolbar, flashing prices and range bars as the equity Market watch, over Delta's public ticker. Unknown values show —, never zero. */
export function CryptoWatch({ symbol, setSymbol, onSelect }: { symbol: string; setSymbol?: (symbol: string) => void; onSelect?: (m: Market) => void }) {
  const [cols, setCols] = useState<ReadonlySet<ColId>>(() => {
    try { const raw = window.localStorage.getItem('crypto.watch.cols'); if (raw) return new Set((JSON.parse(raw) as string[]).filter((c): c is ColId => EXTRA_COLS.some((e) => e.id === c))); } catch { /* default */ }
    return new Set<ColId>();
  });
  const toggleCol = (id: ColId) => setCols((cur) => { const next = new Set(cur); if (next.has(id)) next.delete(id); else next.add(id); try { window.localStorage.setItem('crypto.watch.cols', JSON.stringify([...next])); } catch { /* convenience only */ } return next; });
  const [markets, setMarkets] = useState<Market[]>([]);
  const [loading, setLoading] = useState(true);       // never say "no contracts" while the first request is in flight
  const [error, setError] = useState<{ since: Date; message: string } | null>(null);
  const lastGood = useRef<Date | null>(null);
  const failures = useRef(0);
  const [query, setQuery] = useState('');
  const [mode, setMode] = useState<Mode>('all');
  const [sort, setSort] = useState<{ key: SortKey; dir: 1 | -1 }>({ key: 'volume', dir: -1 });
  const [wanted, setWanted] = useState(true);
  const [starred, setStarred] = useState<string[]>(() => { try { return JSON.parse(window.localStorage.getItem('crypto.watchlist') ?? '[]') as string[]; } catch { return []; } });
  const [, setTick] = useState(0);

  const newest = useRef('');
  const load = useCallback(async () => {
    try {
      // After the first full load only the contracts quoted since the newest one we hold come back, so a one-second refresh stays small.
      const since = newest.current ? `&since=${encodeURIComponent(newest.current)}` : '';
      const result = await apiRequest<{ items: Market[] }>(`/delta-research/markets?limit=500&slim=true${since}`);
      const items = Array.isArray(result?.items) ? result.items : [];
      for (const m of items) if (m.quote_time && m.quote_time > newest.current) newest.current = m.quote_time;
      setMarkets((cur) => {
        if (!since) return items;
        if (items.length === 0) return cur;
        const byId = new Map(cur.map((m) => [m.product_id, m]));
        for (const m of items) byId.set(m.product_id, m);
        return [...byId.values()];
      });
      failures.current = 0; lastGood.current = new Date();
      setError(null);
    } catch (e) {
      failures.current += 1;
      setError((cur) => cur ?? { since: new Date(), message: messageOf(e) });   // one banner, never a new alert per failed poll
    } finally { setLoading(false); }
  }, []);
  useEffect(() => {
    let timer: number | undefined;
    let stopped = false;
    const schedule = () => {
      if (stopped) return;
      const wait = failures.current === 0 ? POLL_MS : BACKOFF_MS[Math.min(failures.current - 1, BACKOFF_MS.length - 1)];
      timer = window.setTimeout(run, wait);
    };
    const run = async () => {
      if (document.hidden) { schedule(); return; }          // no requests while the tab is hidden
      await load(); setTick((n) => n + 1); schedule();
    };
    const onVisible = () => { if (!document.hidden && failures.current === 0) { window.clearTimeout(timer); void run(); } };
    document.addEventListener('visibilitychange', onVisible);
    void run();
    return () => { stopped = true; window.clearTimeout(timer); document.removeEventListener('visibilitychange', onVisible); };
  }, [load]);

  const onStar = useCallback((sym: string) => setStarred((cur) => {
    const next = cur.includes(sym) ? cur.filter((x) => x !== sym) : [...cur, sym];
    try { window.localStorage.setItem('crypto.watchlist', JSON.stringify(next)); } catch { /* per-viewer convenience only */ }
    return next;
  }), []);
  const [depthSymbol, setDepthSymbol] = useState<string | null>(null);   // clicking a perpetual opens its depth panel; closing it closes the socket
  const select = useCallback((m: Market) => { setSymbol?.(m.symbol); onSelect?.(m); setDepthSymbol(m.symbol); }, [setSymbol, onSelect]);

  const live = useMemo(() => markets.filter((m) => m.state === 'live'), [markets]);
  const rows = useMemo(() => {
    const term = query.trim().toUpperCase();
    const out = live.filter((m) => (!term || m.symbol.toUpperCase().includes(term)) && (mode !== 'watchlist' || starred.includes(m.symbol)));
    const key = mode === 'gainers' ? { key: 'chgPct' as SortKey, dir: -1 as const } : mode === 'losers' ? { key: 'chgPct' as SortKey, dir: 1 as const } : sort;
    const val = (m: Market): number | null => {
      const t = m.ticker;
      switch (key.key) {
        case 'last': return num(m.last_price);
        case 'chg': { const l = num(m.last_price); const o = num(t?.open); return l !== null && o !== null ? l - o : null; }
        case 'chgPct': return num(t?.ltp_change_24h);
        case 'volume': return num(t?.turnover_usd ?? m.volume_24h);
        case 'oi': return num(t?.oi_value_usd);
        case 'funding': return num(t?.funding_rate);
        default: return null;
      }
    };
    return [...out].sort((x, y) => {
      if (key.key === 'symbol') return x.symbol.localeCompare(y.symbol) * key.dir;
      const a = val(x); const b = val(y);
      if (a === null && b === null) return 0;
      if (a === null) return 1;            // unknown sorts last, never as zero
      if (b === null) return -1;
      return (a - b) * key.dir;
    });
  }, [live, query, mode, sort, starred]);

  const stats = useMemo(() => {
    let up = 0; let down = 0; let vol = 0; let oi = 0; let ticking = 0;
    for (const m of live) {
      const p = num(m.ticker?.ltp_change_24h);
      if (p !== null) { if (p > 0) up += 1; else if (p < 0) down += 1; }
      vol += num(m.ticker?.turnover_usd ?? m.volume_24h) ?? 0; oi += num(m.ticker?.oi_value_usd) ?? 0;
      if (m.quote_time && Date.now() - new Date(m.quote_time).getTime() < STALE_AFTER_S * 1000) ticking += 1;
    }
    return { up, down, vol, oi, ticking };
  }, [live, rows]);  // eslint-disable-line react-hooks/exhaustive-deps
  const newestQuote = live.reduce((latest, m) => (m.quote_time && m.quote_time > latest ? m.quote_time : latest), '');
  const age = newestQuote ? Math.max(0, Math.round((Date.now() - new Date(newestQuote).getTime()) / 1000)) : null;
  const status = !wanted ? 'paused' : age === null ? 'connecting' : age > STALE_AFTER_S ? 'stale' : 'active';
  const label = status === 'active' ? 'LIVE' : status === 'paused' ? 'FEED PAUSED' : status === 'stale' ? 'STALE' : 'CONNECTING';
  const sortBy = (key: SortKey) => setSort((s) => (s.key === key ? { key, dir: (s.dir * -1) as 1 | -1 } : { key, dir: key === 'symbol' ? 1 : -1 }));
  const arrow = (key: SortKey) => (sort.key === key ? (sort.dir === 1 ? ' ▲' : ' ▼') : '');

  return (
    <div className="watch-page">
      <div className="watch-strip" role="status">
        <span className={`watch-status watch-status-${status === 'paused' ? 'market_closed' : status}`}>● {label}</span>
        <span><b>{rows.length}</b> contracts{rows.length < live.length ? ` of ${live.length}` : ''}</span>
        <span title={'Live ' + stats.ticking + ' · up ' + stats.up + ' · down ' + stats.down + ' · open interest $' + compact(stats.oi)}>24h vol <b>${compact(stats.vol)}</b></span>
        {age !== null && age > STALE_AFTER_S && <span className="watch-missing">newest quote {age < 3600 ? `${Math.round(age / 60)}m` : `${Math.round(age / 3600)}h`} old{!wanted ? ' · feed paused' : ''}</span>}
        <FeedToggle onChange={setWanted} />
        <span className="watch-note">Delta Exchange India · public feed</span>
      </div>
      {error && (
        <p className="alerts-notice" role="alert">
          Research data unavailable since {istTime(error.since)} IST{lastGood.current ? ` · showing last data from ${istTime(lastGood.current)} IST` : ''} · <button type="button" className="link-button" onClick={() => { failures.current = 0; void load(); }}>Retry</button>
        </p>
      )}
      <section className="data-section watch-table-panel" aria-label="Perpetual contracts">
        <div className="watch-toolbar">
          <input type="search" placeholder="Search contract, e.g. SOL" value={query} onChange={(e) => setQuery(e.target.value)} aria-label="Search contracts" />
          <details className="watch-columns"><summary>Columns</summary>
            <div className="watch-columns-menu">{EXTRA_COLS.map((c) => <label key={c.id}><input type="checkbox" checked={cols.has(c.id)} onChange={() => toggleCol(c.id)} /> {c.label}</label>)}</div>
          </details>
          <div className="watch-modes" role="group" aria-label="Filter">
            {([['all', 'All'], ['watchlist', `Watchlist (${starred.length})`], ['gainers', 'Gainers'], ['losers', 'Losers']] as const).map(([id, text]) => (
              <button key={id} type="button" className={mode === id ? 'active' : ''} aria-pressed={mode === id} onClick={() => setMode(id)}>{text}</button>
            ))}
          </div>
        </div>
        <div className="watch-scroll">
          <table className="watch-table">
            <thead><tr>
              <th aria-label="Watchlist" />
              <th><button type="button" onClick={() => sortBy('symbol')}>Contract{arrow('symbol')}</button></th>
              <th className="watch-num"><button type="button" onClick={() => sortBy('last')}>Last{arrow('last')}</button></th>
              {cols.has('chg') && <th className="watch-num"><button type="button" onClick={() => sortBy('chg')}>Chg{arrow('chg')}</button></th>}
              <th className="watch-num"><button type="button" onClick={() => sortBy('chgPct')}>24h %{arrow('chgPct')}</button></th>
              {cols.has('range') && <th>24h range</th>}
              <th className="watch-num"><button type="button" onClick={() => sortBy('volume')}>Volume (USD){arrow('volume')}</button></th>
              {cols.has('oi') && <th className="watch-num"><button type="button" onClick={() => sortBy('oi')}>Open interest (USD){arrow('oi')}</button></th>}
              {cols.has('oi6') && <th className="watch-num">OI 6h</th>}
              <th className="watch-num" title="Funding rate as reported by Delta"><button type="button" onClick={() => sortBy('funding')}>Funding{arrow('funding')}</button></th>
              {cols.has('mark') && <th className="watch-num">Mark</th>}
              {cols.has('bidask') && <th className="watch-num">Bid / Ask</th>}
              {cols.has('updated') && <th className="watch-num">Updated</th>}
            </tr></thead>
            <tbody>
              {rows.map((m) => <Row key={m.product_id} m={m} selected={m.symbol === symbol} starred={starred.includes(m.symbol)} live={wanted} cols={cols} onSelect={select} onStar={onStar} />)}
              {rows.length === 0 && loading && <tr><td colSpan={6 + cols.size} className="table-empty" role="status" aria-busy="true">Loading contracts…</td></tr>}
              {rows.length === 0 && !loading && !error && <tr><td colSpan={6 + cols.size} className="table-empty">{mode === 'watchlist' ? 'No starred contracts yet. Tap ☆ on a row to add it.' : 'No live contracts match. The public feed may be paused or still starting.'}</td></tr>}
            </tbody>
          </table>
        </div>
        <p className="crypto-sub">Delta public data, refreshed every {POLL_MS / 1000} seconds. Unknown values show —.</p>
      </section>
      <DepthPanel symbol={depthSymbol} onClose={() => setDepthSymbol(null)} />
    </div>
  );
}
