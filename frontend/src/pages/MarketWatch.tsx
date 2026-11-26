import { formatISTDate } from '../lib/formatIST';
import { memo, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ApiRequestError, apiRequest } from '../api';
import { DayRange } from '../components/DayRange';
import { TerminalChart, type TerminalCandle } from '../components/TerminalChart';
import type { WorkspaceState } from '../components/useWorkspace';
import { Bell } from 'lucide-react';
import { StockAlerts } from '../components/StockAlerts';
import { StockEvents } from '../components/StockEvents';
import { CommodityWatch } from './CommodityWatch';
import { useWatchlistStream, istDay } from '../hooks/useWatchlistStream';
import { tickPrice } from '../lib/formatPrice';

type UniverseRow = {
  instrument_id: string; symbol: string; name: string; venue: string; sector: string | null;
  last_close: string | null; last_close_date: string | null; volume: string | null;
  previous_close: string | null; change: string | null; change_pct: string | null;
  /** 0 current, 1 = prior close (today's bar not published yet), >= 2 genuinely stale. null when no close is stored. */
  stale: boolean; sessions_behind: number | null; reason: string | null;
  /** false: not served by the Dhan live feed (e.g. a watchlisted symbol outside the NSE universe), so no live ticks. */
  live_feed?: boolean;
  /** The server's newest stored tick, sent only when it is from a later session than the newest daily bar. */
  quote?: { price: string; time: string; session: string; change: string | null; change_pct: string | null; day_high: string | null; day_low: string | null; volume: string | null } | null;
  /** true: added by the user with "Add to market watch" (can be removed again). */
  master_extra?: boolean;
  /** Low/high of the last stored daily bar (that bar's date), shown until a live quote supplies today's range. */
  day_low?: string | null; day_high?: string | null;
};
type Universe = { count: number; instruments: UniverseRow[]; expected_session: string | null; scope?: { instruments: number; stocks: number; funds_and_etfs: number; rule: string }; disclaimer: string };
type FoundInstrument = { id: string; symbol: string; display_name: string; venue: string; currency: string };
type DataSource = { name: string; label: string; available: boolean; enabled: boolean };
type DataSourceSettings = { selected_provider: string | null; sources: DataSource[] };

/** The slice of the shared workspace the watch needs: the same watchlist the Overview page shows. */
export type WatchWorkspace = Pick<WorkspaceState, 'watchlist' | 'addInstrumentToWatchlist' | 'removeInstrumentFromWatchlist' | 'setActiveView' | 'setOpenMarketInstrumentId'>;
type History = { bars: TerminalCandle[] };

type SortKey = 'symbol' | 'ltp' | 'chgPct' | 'volume';
export type PanelTab = 'chart' | 'alerts' | 'events';
export type WatchFocus = { instrumentId: string; tab?: PanelTab; nonce: number };
type Mode = 'all' | 'watchlist' | 'gainers' | 'losers' | 'live';
type ViewRow = UniverseRow & { starred: boolean; hasAlert: boolean; ltp: string | null; chg: string | null; chgPct: string | null; vol: string | null; lo: string | null; hi: string | null; live: boolean; tickAt: number | null; isLive: boolean; quotedAt?: string | null };

const INTERVALS = ['1m', '5m', '15m', '1h', '1d'] as const;
/** Days of history shown per interval (display window only). */
const WINDOW_DAYS: Record<string, number> = { '1m': 2, '5m': 10, '15m': 30, '1h': 90, '1d': 400 };
const RENDER_LIMIT = 400;

const price = (value: string | null) => tickPrice(value);                      // one tick-rounded formatter, shared with Research
const compact = (value: string | null) => (value === null || !Number.isFinite(Number(value)) ? '—'
  : new Intl.NumberFormat('en-IN', { notation: 'compact', maximumFractionDigits: 2 }).format(Number(value)));
const signed = (value: string | null, suffix = '') => (value === null || !Number.isFinite(Number(value)) ? '—'
  : `${Number(value) > 0 ? '+' : ''}${Number(value).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}${suffix}`);
const tone = (value: string | null) => { const n = numeric(value); return n === null || n === 0 ? '' : n > 0 ? 'positive' : 'negative'; };
/** null/empty/non-numeric stay null. Number(null) is 0, which would silently turn "unknown" into zero. */
const numeric = (value: string | null | undefined): number | null => {
  if (value === null || value === undefined || value === '') return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
};
const messageOf = (error: unknown) => (error instanceof ApiRequestError || error instanceof Error ? error.message : 'Request failed.');

/** Levenshtein distance, for the "did you mean" fallback when a search matches nothing. */
function editDistance(a: string, b: string): number {
  const prev = Array.from({ length: b.length + 1 }, (_, j) => j);
  for (let i = 1; i <= a.length; i += 1) {
    let diagonal = prev[0];
    prev[0] = i;
    for (let j = 1; j <= b.length; j += 1) {
      const above = prev[j];
      prev[j] = Math.min(prev[j] + 1, prev[j - 1] + 1, diagonal + (a[i - 1] === b[j - 1] ? 0 : 1));
      diagonal = above;
    }
  }
  return prev[b.length];
}

type RowProps = { row: ViewRow; selected: boolean; onSelect: (id: string) => void; onToggleStar: (row: ViewRow) => void };

const WatchRow = memo(function WatchRow({ row, selected, onSelect, onToggleStar }: RowProps) {
  const previous = useRef<string | null>(row.ltp);
  const [flash, setFlash] = useState<'up' | 'down' | ''>('');
  useEffect(() => {
    if (row.ltp !== null && previous.current !== null && row.ltp !== previous.current) {
      setFlash(Number(row.ltp) > Number(previous.current) ? 'up' : 'down');
      const timer = window.setTimeout(() => setFlash(''), 700);
      previous.current = row.ltp;
      return () => window.clearTimeout(timer);
    }
    previous.current = row.ltp;
    return undefined;
  }, [row.ltp]);
  return (
    <tr className={`${selected ? 'watch-selected' : ''}`} onClick={() => onSelect(row.instrument_id)} aria-selected={selected} title={row.reason ?? undefined}>
      <td className="watch-starcell"><button type="button" className={`watch-star ${row.starred ? 'on' : ''}`} aria-label={row.starred ? `Remove ${row.symbol} from watchlist` : `Add ${row.symbol} to watchlist`} aria-pressed={row.starred} onClick={(event) => { event.stopPropagation(); onToggleStar(row); }}>{row.starred ? '★' : '☆'}</button></td>
      <td className="watch-symbol"><span className={`watch-dot ${row.isLive ? 'live' : ''}`} /><strong>{row.symbol}</strong>{row.hasAlert && <Bell className="watch-alert-dot" size={11} aria-label="has alert" role="img" />}</td>
      <td className="watch-sector">{row.sector ?? '—'}</td>
      <td className={`watch-num watch-ltp ${flash ? `flash-${flash}` : ''}`}>{price(row.ltp)}</td>
      <td className={`watch-num ${tone(row.chg)}`}>{signed(row.chg)}</td>
      <td className={`watch-num ${tone(row.chgPct)}`}>{signed(row.chgPct, '%')}</td>
      <td className="watch-range" aria-label="Day range"><DayRange low={row.lo} high={row.hi} last={row.ltp} /></td>
      <td className="watch-num">{compact(row.vol)}</td>
      <td className={`watch-num watch-age ${!row.live && row.stale ? 'watch-stale' : ''}`}>
        {row.live && row.tickAt ? new Date(row.tickAt).toLocaleTimeString('en-IN', { hour12: false })
          : row.quotedAt ? <span title={`Last price saved by the server: ${new Date(row.quotedAt).toLocaleString('en-IN', { timeZone: 'Asia/Kolkata', hour12: false })} IST`}>{istDay(row.quotedAt) === istDay(Date.now()) ? new Date(row.quotedAt).toLocaleTimeString('en-IN', { timeZone: 'Asia/Kolkata', hour12: false }) : istDay(row.quotedAt)}</span>
          : row.last_close_date ?? '—'}
        {row.live_feed === false && !row.live && <small title="Not on the Dhan live feed: shows the latest stored close">no live feed</small>}
        {!row.live && !row.quotedAt && row.last_close_date && row.stale && <small>stale</small>}
      </td>
    </tr>
  );
});

function EquityWatch({ workspace, initialQuery, searchNonce, focus, onAlertsChanged }: { workspace: WatchWorkspace; initialQuery: string; searchNonce: number; focus?: WatchFocus; onAlertsChanged?: () => void }) {
  const [universe, setUniverse] = useState<UniverseRow[]>([]);
  const [universeSession, setUniverseSession] = useState<string | null>(null);
  const [scope, setScope] = useState<{ instruments: number; stocks: number; funds_and_etfs: number; rule: string } | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [streamError, setStreamError] = useState<string | null>(null);
  const [query, setQuery] = useState('');
  const [sector, setSector] = useState('');
  const [mode, setMode] = useState<Mode>('all');
  const [sort, setSort] = useState<{ key: SortKey; dir: 1 | -1 }>({ key: 'symbol', dir: 1 });
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [interval, setIntervalValue] = useState<(typeof INTERVALS)[number]>('15m');
  const [bars, setBars] = useState<TerminalCandle[]>([]);
  const [chartNote, setChartNote] = useState<string | null>(null);
  const [fill, setFill] = useState<{ done: number; total: number } | null>(null);
  const [found, setFound] = useState<FoundInstrument[] | null>(null);
  const [addingId, setAddingId] = useState<string | null>(null);
  const [panelTab, setPanelTab] = useState<PanelTab>('chart');
  const [alertIds, setAlertIds] = useState<Set<string>>(new Set());
  const loadAlertIds = useCallback(() => apiRequest<{ instrument_id: string | null; enabled: boolean }[]>('/alerts/rules')
    .then((rules) => setAlertIds(new Set(rules.filter((r) => r.enabled && r.instrument_id).map((r) => r.instrument_id as string)))).catch(() => undefined), []);
  useEffect(() => { void loadAlertIds(); }, [loadAlertIds]);
  // a click on the header bell opens that stock here, on the tab it asked for, with no filter hiding it
  useEffect(() => {
    if (!focus) return;
    setQuery(''); setMode('all'); setSector('');
    setSelectedId(focus.instrumentId); setPanelTab(focus.tab ?? 'alerts');
  }, [focus?.nonce]); // eslint-disable-line react-hooks/exhaustive-deps
  const universeIds = useMemo(() => new Set(universe.map((u) => u.instrument_id)), [universe]);
  const outside = useMemo(() => (found ?? []).filter((item) => !universeIds.has(item.id)), [found, universeIds]);
  const starredIds = useMemo(() => new Set(workspace.watchlist.map((entry) => entry.instrument_id)), [workspace.watchlist]);

  const { status, message, quotes } = useWatchlistStream({ universe: true, onError: setStreamError });

  const loadUniverse = useCallback(async () => {
    try {
      const result = await apiRequest<Universe>('/market-data/universe');
      setUniverse(result.instruments);
      setUniverseSession(result.expected_session);
      setScope(result.scope ?? null);
      setError(null);
    } catch (e) { setError(messageOf(e)); } finally { setLoading(false); }
  }, []);
  useEffect(() => { void loadUniverse(); }, [loadUniverse]);
  // the server refreshes today's prices every minute in market hours; pick them up while the page is open (live ticks still apply on top)
  useEffect(() => {
    const timer = window.setInterval(() => { if (!document.hidden) void loadUniverse(); }, 60_000);
    return () => window.clearInterval(timer);
  }, [loadUniverse]);
  // A symbol starred (here or on Overview) after the table loaded must appear without a page reload.
  const requestedMissing = useRef('');
  useEffect(() => {
    if (loading) return;
    const missing = [...starredIds].filter((id) => !universeIds.has(id)).sort().join(',');
    if (!missing || missing === requestedMissing.current) return; // once per distinct set: an unlistable id cannot loop
    requestedMissing.current = missing;
    void loadUniverse();
  }, [starredIds, universeIds, loading, loadUniverse]);

  const rows = useMemo<ViewRow[]>(() => universe.map((u) => {
    // a remembered tick gives way to the stored close once that day's daily bar exists (the official close)
    const seen = quotes[u.instrument_id];
    const q = seen && seen.freshness_status === 'last_seen' && u.last_close_date && u.last_close_date >= istDay(seen.market_timestamp) ? undefined : seen;
    const detail = q?.detail;
    const live = Boolean(q);
    // after a reload there is no tick yet: the server's saved price for the newer session is shown, with its time, never as live
    const saved = u.quote && (!q || (q.freshness_status === 'last_seen' && Date.parse(u.quote.time) > Date.parse(q.market_timestamp))) ? u.quote : null;
    if (saved) {
      return {
        ...u, starred: starredIds.has(u.instrument_id), hasAlert: alertIds.has(u.instrument_id),
        ltp: saved.price, chg: saved.change, chgPct: saved.change_pct, vol: saved.volume, lo: saved.day_low, hi: saved.day_high,
        stale: saved.session === istDay(Date.now()) ? false : u.stale, live: false, isLive: false, tickAt: null, quotedAt: saved.time,
      };
    }
    return {
      ...u, starred: starredIds.has(u.instrument_id), hasAlert: alertIds.has(u.instrument_id),
      ltp: q ? q.close : u.last_close,
      chg: q ? (detail?.absolute_change ?? null) : u.change,
      chgPct: q ? (detail?.percent_change ?? null) : u.change_pct,
      vol: q ? (detail?.bar?.volume ?? u.volume) : u.volume,
      lo: q ? (detail?.bar?.low ?? u.day_low ?? null) : (u.day_low ?? null),
      hi: q ? (detail?.bar?.high ?? u.day_high ?? null) : (u.day_high ?? null),
      live, isLive: q?.freshness_status === 'live', tickAt: q ? q.receivedAt : null,
    };
  }), [universe, quotes, starredIds, alertIds]);

  const sectors = useMemo(() => [...new Set(universe.map((u) => u.sector).filter((s): s is string => Boolean(s)))].sort(), [universe]);

  // Matches that the Watchlist/Gainers/Live tab or the sector filter hides: a search must never look empty while the matches exist.
  const hiddenByFilter = useMemo(() => {
    const term = query.trim().toUpperCase();
    if (!term || (mode === 'all' && !sector)) return [] as ViewRow[];
    return rows.filter((r) => r.symbol.toUpperCase().includes(term) || r.name.toUpperCase().includes(term));
  }, [rows, query, mode, sector]);
  const { view, fuzzy } = useMemo(() => {
    const term = query.trim().toUpperCase();
    const passes = (r: ViewRow) => (!sector || r.sector === sector) && (mode === 'all' || (mode === 'watchlist' && r.starred) || (mode === 'live' && r.live) || (mode === 'gainers' && (numeric(r.chgPct) ?? 0) > 0) || (mode === 'losers' && (numeric(r.chgPct) ?? 0) < 0));
    const exact = rows.filter((r) => (!term || r.symbol.toUpperCase().includes(term) || r.name.toUpperCase().includes(term)) && passes(r));
    // A typo must not dead-end: with no exact match, offer the closest symbols (by edit distance) instead.
    let filtered = exact;
    let isFuzzy = false;
    if (term.length >= 3 && exact.length === 0) {
      const allowed = term.length >= 5 ? 2 : 1;
      filtered = rows
        .map((r) => ({ r, d: editDistance(term, r.symbol.toUpperCase()) }))
        .filter(({ r, d }) => d <= allowed && passes(r))
        .sort((x, y) => x.d - y.d || x.r.symbol.localeCompare(y.r.symbol))
        .slice(0, 8)
        .map(({ r }) => r);
      isFuzzy = filtered.length > 0;
    }
    const num = (r: ViewRow) => (sort.key === 'ltp' ? r.ltp : sort.key === 'chgPct' ? r.chgPct : r.vol);
    if (isFuzzy) return { view: filtered, fuzzy: true }; // keep closest-first order
    return { fuzzy: false, view: filtered.sort((a, b) => {
      if (sort.key === 'symbol') return a.symbol.localeCompare(b.symbol) * sort.dir;
      const x = numeric(num(a)), y = numeric(num(b));
      if (x === null || y === null) return x === y ? 0 : x === null ? 1 : -1; // unknown values always sink to the bottom
      return (x - y) * sort.dir;
    }) };
  }, [rows, query, sector, mode, sort]);

  // the header counts what the table actually shows (after the filters and the render limit), and names the stocks that have no current close instead of leaving silent gaps
  const shown = view.slice(0, RENDER_LIMIT);
  const noClose = rows.filter((r) => r.ltp === null).map((r) => r.symbol);
  const stats = useMemo(() => ({
    ticking: shown.filter((r) => r.live).length,
    // advance/decline counts use CURRENT closes only: a stale close (the API's own `stale` flag) says nothing about today
    up: shown.filter((r) => (r.live || !r.stale) && (numeric(r.chgPct) ?? 0) > 0).length,
    down: shown.filter((r) => (r.live || !r.stale) && (numeric(r.chgPct) ?? 0) < 0).length,
    excluded: shown.filter((r) => !r.live && r.stale && r.last_close !== null).length,
    staleAsOf: shown.filter((r) => !r.live && r.stale && r.last_close_date).map((r) => r.last_close_date as string).sort().pop() ?? null,
    missing: rows.filter((r) => !r.live && r.stale).length,          // the same flag the API reports: header, button and rows can no longer disagree
  }), [rows, shown]);

  const selected = rows.find((r) => r.instrument_id === selectedId) ?? null;

  useEffect(() => {
    if (!selected) { setBars([]); return undefined; }
    let cancelled = false;
    setChartNote(null);
    const end = new Date();
    const start = new Date(end.getTime() - WINDOW_DAYS[interval] * 86_400_000);
    const params = new URLSearchParams({ interval, start: start.toISOString(), end: end.toISOString(), limit: '500' });
    apiRequest<History>(`/market-data/instruments/${selected.instrument_id}/history?${params}`)
      .then((h) => { if (!cancelled) { setBars(h.bars); if (h.bars.length === 0) setChartNote('No stored bars for this window. Use “Load history” to fetch them from the broker.'); } })
      .catch((e: unknown) => { if (!cancelled) { setBars([]); setChartNote(messageOf(e)); } });
    return () => { cancelled = true; };
  }, [selected?.instrument_id, interval]); // eslint-disable-line react-hooks/exhaustive-deps

  async function loadHistory() {
    if (!selected) return;
    setChartNote('Loading history from the broker…');
    const end = new Date();
    const start = new Date(end.getTime() - WINDOW_DAYS[interval] * 86_400_000);
    const params = new URLSearchParams({ interval, start: start.toISOString(), end: end.toISOString(), limit: '500' });
    try {
      const h = await apiRequest<History>(`/market-data/instruments/${selected.instrument_id}/history/sync?${params}`, { method: 'POST' });
      setBars(h.bars);
      setChartNote(h.bars.length ? null : 'The broker returned no bars for this window.');
    } catch (e) { setChartNote(messageOf(e)); }
  }

  // Off-hours the table can only show what is stored. This fetches the last sessions' real daily closes for
  // symbols that have none (3 at a time), then reloads the universe. Nothing is estimated.
  async function fillMissingCloses() {
    const targets = universe.filter((u) => u.stale);                  // exactly the rows the API marks stale
    setFill({ done: 0, total: targets.length });
    const end = new Date();
    const start = new Date(end.getTime() - 12 * 86_400_000);
    const params = new URLSearchParams({ interval: '1d', start: start.toISOString(), end: end.toISOString(), limit: '30' });
    let index = 0, done = 0;
    const worker = async () => {
      while (index < targets.length) {
        const target = targets[index++];
        try { await apiRequest(`/market-data/instruments/${target.instrument_id}/history/sync?${params}`, { method: 'POST' }); } catch { /* shown as still missing */ }
        done += 1;
        setFill({ done, total: targets.length });
      }
    };
    await Promise.all([worker(), worker(), worker()]);
    await loadUniverse();
    setFill(null);
  }

  async function addToMarketWatch(item: FoundInstrument) {
    setAddingId(item.id);
    try {
      await apiRequest<void>(`/market-data/universe/extras/${item.id}`, { method: 'POST' });
      await loadUniverse();
      setQuery(item.symbol);      // show the new row, ready to be starred
      setSelectedId(item.id);
    } catch (e) { setError(messageOf(e)); } finally { setAddingId(null); }
  }
  async function removeFromMarketWatch(row: ViewRow) {
    try {
      await apiRequest<void>(`/market-data/universe/extras/${row.instrument_id}`, { method: 'DELETE' });
      if (selectedId === row.instrument_id) setSelectedId(null);
      await loadUniverse();
    } catch (e) { setError(messageOf(e)); }
  }
  const toggleStar = useCallback((row: ViewRow) => {
    if (row.starred) void workspace.removeInstrumentFromWatchlist(row.instrument_id);
    else void workspace.addInstrumentToWatchlist(row.instrument_id, row.symbol);
  }, [workspace]);

  // One search box: it filters the table, and (debounced) also looks the term up across every listed instrument so
  // anything that is not in the table can still be added to the watchlist.
  useEffect(() => {
    const term = query.trim();
    if (term.length < 2) { setFound(null); return undefined; }
    let cancelled = false;
    const timer = window.setTimeout(() => {
      apiRequest<FoundInstrument[]>(`/market-data/instruments?${new URLSearchParams({ query: term, limit: '10' })}`)
        .then((items) => { if (!cancelled) setFound(items); })
        .catch(() => { if (!cancelled) setFound([]); });
    }, 350);
    return () => { cancelled = true; window.clearTimeout(timer); };
  }, [query]);
  // The header search lands here with its term.
  useEffect(() => {
    if (initialQuery.trim()) setQuery(initialQuery);
  }, [initialQuery, searchNonce]);

  const sortBy = (key: SortKey) => setSort((s) => (s.key === key ? { key, dir: (s.dir * -1) as 1 | -1 } : { key, dir: key === 'symbol' ? 1 : -1 }));
  const arrow = (key: SortKey) => (sort.key === key ? (sort.dir === 1 ? ' ▲' : ' ▼') : '');
  const statusLabel = status === 'active' ? 'LIVE' : status === 'market_closed' ? 'MARKET CLOSED' : status === 'connecting' || status === 'reconnecting' ? 'CONNECTING' : status === 'stale' ? 'STALE' : status === 'unavailable' ? 'FEED UNAVAILABLE' : 'IDLE';

  return (
    <div className="watch-page">
      <div className="watch-strip" role="status">
        <span className={`watch-status watch-status-${status}`}>● {statusLabel}</span>
        <span title={scope?.rule ?? `${universe.length} in the master list`}><b>{shown.length}</b> instruments{shown.length < universe.length ? ` of ${universe.length}` : ''}</span>
        <span className="positive">▲ <b>{stats.up}</b></span>
        <span className="negative">▼ <b>{stats.down}</b></span>
        {stats.missing > 0 && <span className="watch-missing" title={[noClose.length ? `No stored or live close: ${noClose.join(', ')}` : '', stats.excluded ? `${stats.excluded} stale closes not counted in ▲ ▼${stats.staleAsOf ? ` (as of ${formatISTDate(stats.staleAsOf)})` : ''}` : ''].filter(Boolean).join(' · ') || undefined}>{stats.missing} need a refreshed close</span>}
        {stats.missing === 0 && noClose.length > 0 && <span className="watch-missing" title={noClose.join(', ')}>{noClose.length} without a close</span>}
        <span className="watch-note">{universeSession && <>Last full session <b>{universeSession}</b> · </>}decision support, not investment advice</span>
      </div>
      {(error || streamError || (message && status !== 'active')) && <p className="alerts-notice" role="alert">{error ?? streamError ?? message}</p>}

      <div className={selected ? 'watch-split' : 'watch-split watch-split-full'}>
        <section className="data-section watch-table-panel" aria-label="Market watch">
          <div className="watch-toolbar">
            <input type="search" placeholder="Search symbol or name (also finds instruments to add)" value={query} onChange={(e) => setQuery(e.target.value)} aria-label="Search symbols" />
            <select value={sector} onChange={(e) => setSector(e.target.value)} aria-label="Sector"><option value="">All sectors</option>{sectors.map((s) => <option key={s} value={s}>{s}</option>)}</select>
            <div className="watch-modes" role="group" aria-label="Filter">
              {(['all', 'watchlist', 'gainers', 'losers', 'live'] as Mode[]).map((m) => <button key={m} type="button" className={mode === m ? 'active' : ''} aria-pressed={mode === m} onClick={() => setMode(m)}>{m[0].toUpperCase() + m.slice(1)}</button>)}
            </div>
            <button className="secondary-button" type="button" onClick={() => void fillMissingCloses()} disabled={fill !== null || stats.missing === 0}>
              {fill ? `Loading closes ${fill.done}/${fill.total}…` : `Load missing closes (${stats.missing})`}
            </button>
          </div>
          {outside.length > 0 && (
            <ul className="watch-find-results" aria-label="Not in the market table">
              <li className="watch-find-head">Not in the market watch — add it here first, then ★ it for your watchlist</li>
              {outside.map((item) => (
                <li key={item.id}>
                  <span><strong>{item.symbol}</strong> · {item.venue}<small>{item.display_name}</small></span>
                  <button type="button" className="secondary-button" disabled={addingId === item.id} onClick={() => void addToMarketWatch(item)}>
                    {addingId === item.id ? 'Adding…' : '＋ Add to market watch'}
                  </button>
                </li>
              ))}
            </ul>
          )}
          {view.length === 0 && hiddenByFilter.length > 0 && (
            <p className="watch-fuzzy" role="status">{hiddenByFilter.length} match{hiddenByFilter.length === 1 ? '' : 'es'} for “{query.trim()}” {hiddenByFilter.length === 1 ? 'is' : 'are'} hidden by the {mode !== 'all' ? `${mode} tab` : ''}{mode !== 'all' && sector ? ' and the ' : ''}{sector ? 'sector filter' : ''}. <button type="button" className="secondary-button" onClick={() => { setMode('all'); setSector(''); }}>Show all</button></p>
          )}
          {fuzzy && <p className="watch-fuzzy" role="status">No exact match for “{query.trim()}” — showing the closest symbols.</p>}
          {loading ? <p className="table-empty">Loading the market universe…</p> : (
            <div className="watch-scroll">
              <table className="watch-table equity-table">
                <thead><tr>
                  <th aria-label="Watchlist" />
                  <th><button type="button" onClick={() => sortBy('symbol')}>Symbol{arrow('symbol')}</button></th>
                  <th>Sector</th>
                  <th className="watch-num"><button type="button" onClick={() => sortBy('ltp')}>LTP{arrow('ltp')}</button></th>
                  <th className="watch-num">Chg</th>
                  <th className="watch-num"><button type="button" onClick={() => sortBy('chgPct')}>Chg %{arrow('chgPct')}</button></th>
                  <th>Day range</th>
                  <th className="watch-num"><button type="button" onClick={() => sortBy('volume')}>Volume{arrow('volume')}</button></th>
                  <th className="watch-num">Last tick / close</th>
                </tr></thead>
                <tbody>{shown.map((r) => <WatchRow key={r.instrument_id} row={r} selected={r.instrument_id === selectedId} onSelect={setSelectedId} onToggleStar={toggleStar} />)}</tbody>
              </table>
              {view.length > RENDER_LIMIT && <p className="crypto-sub">Showing the first {RENDER_LIMIT} of {view.length}. Narrow the search to see the rest.</p>}
              {view.length === 0 && <p className="table-empty">No symbols match these filters.</p>}
            </div>
          )}
        </section>

        {selected && <aside className="data-section watch-chart-panel" aria-label="Chart">
            <>
              <div className="section-heading">
                <div><p className="eyebrow">{selected.venue} · {selected.sector ?? 'Equity'}</p><h3>{selected.symbol}</h3>{selected.name !== selected.symbol && <p className="watch-company">{selected.name}</p>}</div>
                <button type="button" className="secondary-button watch-close" aria-label="Close chart" onClick={() => setSelectedId(null)}>Close ✕</button>
                <div className="crypto-resolutions" role="group" aria-label="Candle size">
                  {INTERVALS.map((i) => <button key={i} type="button" className={i === interval ? 'active' : ''} aria-pressed={i === interval} onClick={() => setIntervalValue(i)}>{i}</button>)}
                </div>
              </div>
              <p className="watch-quote"><span className="watch-big">{price(selected.ltp)}</span> <span className={tone(selected.chgPct)}>{signed(selected.chg)} ({signed(selected.chgPct, '%')})</span></p>
              <nav className="panel-tabs" role="tablist" aria-label="Stock details">
                <button type="button" role="tab" aria-selected={panelTab === 'chart'} className={panelTab === 'chart' ? 'active' : ''} onClick={() => setPanelTab('chart')}>Chart</button>
                <button type="button" role="tab" aria-selected={panelTab === 'alerts'} className={panelTab === 'alerts' ? 'active' : ''} onClick={() => setPanelTab('alerts')}><Bell size={12} aria-hidden="true" /> Alerts{selected.hasAlert ? ' ●' : ''}</button>
                <button type="button" role="tab" aria-selected={panelTab === 'events'} className={panelTab === 'events' ? 'active' : ''} onClick={() => setPanelTab('events')}>Corporate actions &amp; news</button>
              </nav>
              {panelTab === 'alerts' && <StockAlerts instrumentId={selected.instrument_id} symbol={selected.symbol} lastPrice={selected.ltp} onChanged={() => { void loadAlertIds(); onAlertsChanged?.(); }} />}
              {panelTab === 'events' && <StockEvents instrumentId={selected.instrument_id} symbol={selected.symbol} />}
              {panelTab === 'chart' && <>
              {bars.length > 0 ? <TerminalChart candles={bars} daily={interval === '1d'} height={360} ariaLabel={`${selected.symbol} candles`} /> : <div className="table-empty">{chartNote ?? 'Loading…'}</div>}
              <div className="crypto-chart-actions">
                {selected.master_extra && <button className="secondary-button" type="button" onClick={() => void removeFromMarketWatch(selected)}>Remove from market watch</button>}
                <button className="secondary-button" type="button" onClick={() => toggleStar(selected)}>{selected.starred ? '★ Remove from watchlist' : '☆ Add to watchlist'}</button>
                <button className="secondary-button" type="button" onClick={() => void loadHistory()}>Load history</button>
                <button className="secondary-button" type="button" onClick={() => { workspace.setOpenMarketInstrumentId(selected.instrument_id); workspace.setActiveView('market-data'); }}>Full details ›</button>
                <button className="secondary-button" type="button" onClick={() => setPanelTab('alerts')}><Bell size={13} aria-hidden="true" /> Alert</button>
                <button className="secondary-button" type="button" onClick={() => setPanelTab('events')}>Actions &amp; news</button>
                {chartNote && bars.length > 0 && <span className="crypto-sub">{chartNote}</span>}
              </div>
              </>}
              {selected.reason && <p className="crypto-sub">Day change unavailable: {selected.reason}.</p>}
            </>
        </aside>}
      </div>
    </div>
  );
}

/** One place for NSE equities and MCX commodities: live prices, charts, the Overview watchlist, and the data source. */
export function MarketWatch({ workspace, initialQuery = '', searchNonce = 0, focus, onAlertsChanged }: { workspace: WatchWorkspace; initialQuery?: string; searchNonce?: number; focus?: WatchFocus; onAlertsChanged?: () => void }) {
  const [section, setSection] = useState<'equities' | 'commodities'>('equities');
  const [sources, setSources] = useState<DataSourceSettings | null>(null);
  const [sourceError, setSourceError] = useState<string | null>(null);
  useEffect(() => { if (focus) setSection('equities'); }, [focus?.nonce]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    apiRequest<DataSourceSettings>('/market-data/settings').then(setSources).catch((e: unknown) => setSourceError(messageOf(e)));
  }, []);
  async function chooseSource(provider: string) {
    setSourceError(null);
    try { setSources(await apiRequest<DataSourceSettings>('/market-data/settings/provider', { method: 'PUT', body: JSON.stringify({ provider }) })); }
    catch (e) { setSourceError(messageOf(e)); }
  }
  const eligible = (sources?.sources ?? []).filter((source) => source.available && source.enabled);

  return (
    <main className="watch-shell">
      <div className="watch-head">
        <nav className="watch-tabs" role="tablist" aria-label="Market">
          <button type="button" role="tab" aria-selected={section === 'equities'} className={section === 'equities' ? 'active' : ''} onClick={() => setSection('equities')}>Equities · NSE</button>
          <button type="button" role="tab" aria-selected={section === 'commodities'} className={section === 'commodities' ? 'active' : ''} onClick={() => setSection('commodities')}>Commodities · MCX</button>
        </nav>
        {eligible.length > 0 && (
          <label className="watch-source">Data source
            <select value={sources?.selected_provider ?? eligible[0].name} onChange={(e) => void chooseSource(e.target.value)} aria-label="Data source">
              {eligible.map((source) => <option key={source.name} value={source.name}>{source.label}</option>)}
            </select>
          </label>
        )}
      </div>
      {sourceError && <p className="alerts-notice" role="alert">{sourceError}</p>}
      {section === 'equities' ? <EquityWatch workspace={workspace} initialQuery={initialQuery} searchNonce={searchNonce} focus={focus} onAlertsChanged={onAlertsChanged} /> : <CommodityWatch />}
    </main>
  );
}
