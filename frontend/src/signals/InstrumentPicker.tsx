import { useCallback, useEffect, useId, useMemo, useRef, useState } from 'react';
import { apiRequest } from '../api';
import { messageOf } from './lab';
import { dataState } from './preflight';
import type { BackfillJob, InstrumentRow, Market } from './types';

type Candidate = { id: string; symbol: string; name: string; venue?: string };
type RemoteInstrument = { id: string; symbol: string; display_name?: string; name?: string; venue?: string; country?: string; asset_class?: string };
const SEARCH_DEBOUNCE_MS = 300;
const POLL_MS = 2000;
const MAX_RESULTS = 30;

function Countdown({ until }: { until: string }) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => { const t = window.setInterval(() => setNow(Date.now()), 1000); return () => window.clearInterval(t); }, []);
  return <>{Math.max(0, Math.ceil((new Date(until).getTime() - now) / 1000))}s</>;
}

/** Searchable multi-select over every instrument the app knows. Each row shows its stored-bar state; rows without data offer Fetch (existing 1m backfill). */
export function InstrumentPicker({ stored, market, chosen, onChange, window: win, onStoredChanged }: {
  stored: InstrumentRow[]; market: Market; chosen: string[]; onChange: (ids: string[]) => void; window: { start: string; end: string }; onStoredChanged: () => void;
}) {
  const [query, setQuery] = useState('');
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const [remote, setRemote] = useState<Candidate[]>([]);
  const [searching, setSearching] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);
  const [job, setJob] = useState<BackfillJob | null>(null);
  const [fetchError, setFetchError] = useState<string | null>(null);
  const [fetching, setFetching] = useState<Set<string>>(new Set());
  const root = useRef<HTMLDivElement>(null);
  const listId = useId();
  const known = useRef(new Map<string, Candidate>());

  const barsOf = useMemo(() => new Map(stored.map((i) => [i.id, i.bars_1m])), [stored]);
  for (const i of stored) known.current.set(i.id, { id: i.id, symbol: i.symbol, name: i.name });

  // Local matches are instant; the provider search (the same endpoint as the global search bar) adds instruments we have no bars for yet.
  useEffect(() => {
    const q = query.trim();
    if (!q) { setRemote([]); setSearchError(null); return undefined; }
    let live = true;
    setSearching(true);
    const timer = window.setTimeout(() => {
      apiRequest<RemoteInstrument[]>(`/market-data/instruments?query=${encodeURIComponent(q)}&limit=20`)
        .then((rows) => { if (live) { setRemote(rows.map((r) => ({ id: r.id, symbol: r.symbol, name: r.display_name ?? r.name ?? r.symbol, venue: r.venue }))); setSearchError(null); } })
        .catch((e: unknown) => { if (live) { setRemote([]); setSearchError(messageOf(e)); } })
        .finally(() => { if (live) setSearching(false); });
    }, SEARCH_DEBOUNCE_MS);
    return () => { live = false; window.clearTimeout(timer); };
  }, [query]);

  const results: Candidate[] = useMemo(() => {
    const q = query.trim().toLowerCase();
    const local = stored.filter((i) => i.market === market && (!q || i.symbol.toLowerCase().includes(q) || i.name.toLowerCase().includes(q)))
      .map((i) => ({ id: i.id, symbol: i.symbol, name: i.name }));
    const seen = new Set(local.map((c) => c.id));
    const extra = remote.filter((c) => !seen.has(c.id));
    for (const c of extra) known.current.set(c.id, c);
    return [...local, ...extra].slice(0, MAX_RESULTS);
  }, [query, stored, remote, market]);

  useEffect(() => { setActive(0); }, [query]);
  useEffect(() => {
    if (!open) return undefined;
    const close = (e: MouseEvent) => { if (!root.current?.contains(e.target as Node)) setOpen(false); };
    document.addEventListener('mousedown', close);
    return () => document.removeEventListener('mousedown', close);
  }, [open]);

  const toggle = (id: string) => onChange(chosen.includes(id) ? chosen.filter((x) => x !== id) : [...chosen, id]);
  const nameOf = (id: string) => known.current.get(id)?.symbol ?? id.slice(0, 8);

  // ── fetch on demand: the existing batch backfill endpoint, polled through the sync-jobs endpoint ──
  useEffect(() => {
    if (!job || job.status === 'completed' || job.status === 'failed') return undefined;
    const timer = window.setInterval(() => {
      apiRequest<BackfillJob>(`/market-data/history/sync-jobs/${job.id}`).then(setJob).catch((e: unknown) => setFetchError(messageOf(e)));
    }, POLL_MS);
    return () => window.clearInterval(timer);
  }, [job]);
  const finished = useRef<string | null>(null);
  useEffect(() => {
    if (job && (job.status === 'completed' || job.status === 'failed') && finished.current !== job.id) {
      finished.current = job.id;
      setFetching(new Set());
      onStoredChanged();
    }
  }, [job, onStoredChanged]);

  const fetchOne = useCallback(async (id: string) => {
    setFetchError(null);
    try {
      const started = await apiRequest<BackfillJob>('/market-data/backfill/intraday-1m', { method: 'POST', body: JSON.stringify({ instrument_ids: [id], start: win.start, end: win.end }) });
      if (started.already_running && !started.instruments.some((i) => i.instrument_id === id)) {
        setFetchError('Another 1-minute backfill is already running. Wait for it to finish, then fetch again.');
        return;
      }
      setFetching((cur) => new Set(cur).add(id));
      setJob(started);
    } catch (e) { setFetchError(messageOf(e)); }
  }, [win.start, win.end]);

  const rowState = (c: Candidate) => {
    const item = job?.instruments.find((i) => i.instrument_id === c.id);
    const active_ = fetching.has(c.id) && item && !['completed', 'failed', 'unrecoverable_gaps'].includes(item.status);
    return { item, fetchingNow: !!(fetching.has(c.id) && (!item || active_)) };
  };

  const onKey = (e: React.KeyboardEvent) => {
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') { e.preventDefault(); setOpen(true); setActive((a) => Math.min(results.length - 1, Math.max(0, a + (e.key === 'ArrowDown' ? 1 : -1)))); }
    else if (e.key === 'Enter') { e.preventDefault(); const c = results[active]; if (open && c) toggle(c.id); }
    else if (e.key === 'Escape') setOpen(false);
    else if (e.key === 'Backspace' && !query && chosen.length) onChange(chosen.slice(0, -1));
  };

  return (
    <div className="sl-picker" ref={root}>
      <div className="sl-chips" aria-label="Selected instruments">
        {chosen.map((id) => <button type="button" key={id} className="sl-chip" onClick={() => toggle(id)} title="Remove" aria-label={`Remove ${nameOf(id)}`}>{nameOf(id)} ✕</button>)}
      </div>
      <input className="sl-picker-input" role="combobox" aria-label="Search instruments" aria-expanded={open} aria-controls={listId} aria-autocomplete="list" placeholder="Search by symbol or name…"
        value={query} onChange={(e) => { setQuery(e.target.value); setOpen(true); }} onFocus={() => setOpen(true)} onKeyDown={onKey} />
      {fetchError && <p className="sl-picker-error" role="alert">{fetchError}</p>}
      {open && (
        <ul className="sl-picker-list" id={listId} role="listbox" aria-label="Instruments" aria-multiselectable="true">
          {results.map((c, i) => {
            const bars = barsOf.get(c.id) ?? 0;
            const state = dataState(bars);
            const { item, fetchingNow } = rowState(c);
            const selected = chosen.includes(c.id);
            return (
              <li key={c.id} role="option" aria-selected={selected} className={`${i === active ? 'sl-active' : ''}${selected ? ' sl-selected' : ''}`} onMouseEnter={() => setActive(i)} onMouseDown={(e) => { e.preventDefault(); toggle(c.id); }}>
                <span className="sl-pick-sym">{selected ? '✓ ' : ''}{c.symbol}<small className="sl-muted"> {c.name !== c.symbol ? c.name : ''}</small></span>
                <span className="sl-pick-state">
                  {fetchingNow ? (
                    item?.status === 'waiting_rate_limit' && job?.pause
                      ? <span>Dhan rate limit · waiting <strong><Countdown until={job.pause.until} /></strong> (retry {job.pause.attempt} of {job.pause.of})</span>
                      : <span>Fetching… {item?.before?.missing_pct != null ? `${item.before.missing_pct}% missing` : ''}</span>
                  ) : state === 'none' ? (
                    <><span className="sl-muted">no 1m data</span> · <button type="button" className="sl-link" aria-label={`Fetch ${c.symbol}`} onMouseDown={(e) => { e.stopPropagation(); e.preventDefault(); void fetchOne(c.id); }}>Fetch</button></>
                  ) : (
                    <span>{bars.toLocaleString()} bars · <span className={state === 'thin' ? 'sl-warnText' : ''}>{state === 'thin' ? 'thin history' : 'ready'}</span></span>
                  )}
                  {item && !fetchingNow && item.status === 'unrecoverable_gaps' && <small className="sl-muted"> provider has no data for some minutes: {item.unrecoverable[0]?.reason ?? 'recorded by the provider'}</small>}
                  {item && !fetchingNow && item.status === 'failed' && <small className="sl-picker-error"> {item.reason ?? 'fetch failed'}</small>}
                  {item && !fetchingNow && (item.status === 'completed' || item.status === 'unrecoverable_gaps') && item.after?.missing_pct != null && <small className="sl-muted"> fetched · {item.before?.missing_pct ?? '—'}% → {item.after.missing_pct}% missing</small>}
                </span>
              </li>
            );
          })}
          {searching && <li className="sl-picker-note" role="presentation">Searching…</li>}
          {!searching && results.length === 0 && <li className="sl-picker-note" role="presentation">{query.trim() ? `No instrument matches “${query.trim()}”.` : 'Type to search every instrument.'}</li>}
          {searchError && <li className="sl-picker-note sl-picker-error" role="presentation">Provider search failed: {searchError}. Showing stored instruments only.</li>}
        </ul>
      )}
    </div>
  );
}
