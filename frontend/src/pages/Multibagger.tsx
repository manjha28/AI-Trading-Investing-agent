import { useCallback, useEffect, useMemo, useState } from 'react';
import { apiRequest } from '../api';
import { Banner, Button, Chip, DataTable, EmptyState, ScoreBar, Sheet, Tabs, type Column } from '../components/ui/kit';
import { formatIST } from '../lib/format';

type Item = {
  last_close?: string | null; last_close_date?: string | null;
  symbol: string; instrument_id: string; class: string; rank: number | null; scores: Record<string, number | null>; checklist: string | null; checks_passed?: number | null; checks_total?: number | null;
  unknown_count?: number; unknown_ids?: string[]; fail_count?: number; timing: string | null; kind: string | null; reasons: string[]; class_if_unknown_checks_cleared: string | null;
};
type Resp = { universe_rule?: string; enabled: boolean; shadow?: boolean; message?: string; as_of?: string | null; model_version?: string | null; counts?: Record<string, number>; items: Item[]; disclaimer: string };
type Check = { id: string; stage: string; value: unknown; reason: string; result: 'pass' | 'fail' | 'unknown' };
type Detail = { symbol: string; class: string; as_of: string; scores: Record<string, number | null>; reasons: string[]; checks: Check[]; checklist: string | null; timing: string | null; kind: string | null; class_if_unknown_checks_cleared: { class?: string; note?: string } | null };

const CLASS_ORDER = ['multibagger_candidate', 'quality_compounder', 'emerging', 'new_listing', 'watch', 'not_a_fit', 'not_tradable', 'avoid', 'not_assessable'];
const EXCLUDED = new Set(['avoid', 'not_tradable', 'not_assessable']);
const LONG: Record<string, string> = {
  multibagger_candidate: 'Multibagger candidate: passes the quality, growth, valuation, timing and veto checks', quality_compounder: 'Quality compounder: strong, proven business; not yet a candidate',
  emerging: 'Emerging: unproven, about one year of evidence (half size)', new_listing: 'New listing: too little history (quarter size)', watch: 'Watch: some checks pass; others are failed or unknown',
  not_a_fit: 'Not a fit for the long-term screen', not_tradable: 'Not tradable: fails liquidity or surveillance', avoid: 'Avoid: a veto check failed', not_assessable: 'Not assessable: too little stored data to score',
};
const GAP_WORDS: Record<string, string> = { pledge: 'pledge', surveillance: 'ASM/GSM', auditor_or_regulatory: 'auditor' };
const SCORE_KEYS = ['Q', 'G', 'V', 'T', 'X'] as const;
const NA_REASON = 'No score: fewer than 70% of its inputs are stored';
const DISMISS_KEY = 'mb.shadow.dismissed';
const PRICE_KEY = 'mb.price';
export const PRICE_PRESETS: { label: string; max: string }[] = [{ label: 'Any price', max: '' }, { label: 'Below ₹20', max: '20' }, { label: 'Below ₹50', max: '50' }, { label: 'Below ₹100', max: '100' }, { label: 'Below ₹500', max: '500' }];
/** The price filter lives in the URL (price_min, price_max) and local storage; the URL wins when both exist. */
function initialPrice(): { min: string; max: string } {
  try {
    const q = new URLSearchParams(window.location.search);
    if (q.has('price_min') || q.has('price_max')) return { min: q.get('price_min') ?? '', max: q.get('price_max') ?? '' };
    const saved = JSON.parse(window.localStorage.getItem(PRICE_KEY) ?? 'null') as { min?: string; max?: string } | null;
    return { min: saved?.min ?? '', max: saved?.max ?? '' };
  } catch { return { min: '', max: '' }; }
}
export function withinPrice(close: string | null | undefined, min: string, max: string): boolean {
  if (min === '' && max === '') return true;
  if (close == null || close === '') return false;           // unknown price never passes a price filter
  const v = Number(close);
  return (min === '' || v >= Number(min)) && (max === '' || v <= Number(max));
}

// The response is kept for the session: sorting and filtering are client-side, so a tab switch never refetches.
let cache: Resp | null = null;
export function clearMultibaggerCache() { cache = null; }

const nice = (id: string) => GAP_WORDS[id] ?? id.replace(/_/g, ' ');
const list = (words: string[]) => (words.length <= 1 ? words.join('') : `${words.slice(0, -1).join(', ')} and ${words[words.length - 1]}`);
const orderOf = (c: string) => { const i = CLASS_ORDER.indexOf(c); return i < 0 ? CLASS_ORDER.length : i; };

export function Multibagger({ onOpen }: { onOpen?: (symbol: string) => void }) {
  const [data, setData] = useState<Resp | null>(cache);
  const [error, setError] = useState<unknown>(null);
  const [klass, setKlass] = useState('all');
  const [showExcluded, setShowExcluded] = useState(false);
  const [query, setQuery] = useState('');
  const [buyZone, setBuyZone] = useState(false);
  const [fewUnknown, setFewUnknown] = useState(false);
  const [minQ, setMinQ] = useState(0);
  const [price, setPrice] = useState(initialPrice);
  const [selected, setSelected] = useState<string | null>(null);
  const [dismissed, setDismissed] = useState(() => { try { return window.sessionStorage.getItem(DISMISS_KEY) === '1'; } catch { return false; } });

  const load = useCallback(() => {
    setError(null);
    apiRequest<Resp>('/research/multibagger?limit=1000').then((r) => { cache = r; setData(r); }).catch((e: unknown) => setError(e));
  }, []);
  useEffect(() => { if (!cache) load(); }, [load]);

  const items = useMemo(() => data?.items ?? [], [data]);
  const counts = data?.counts ?? {};
  const hasRank = items.some((i) => i.rank !== null && i.rank !== undefined);
  const gaps = useMemo(() => {                                    // data gaps shared by (nearly) every stock are said once, not once per row
    const seen: Record<string, number> = {};
    for (const i of items) for (const id of i.unknown_ids ?? []) seen[id] = (seen[id] ?? 0) + 1;
    return Object.keys(seen).filter((id) => items.length > 0 && seen[id] / items.length >= 0.9).sort((a, b) => seen[b] - seen[a]);
  }, [items]);

  const rows = useMemo(() => {
    const term = query.trim().toUpperCase();
    return items
      .filter((i) => (klass === 'all' ? showExcluded || !EXCLUDED.has(i.class) : i.class === klass))
      .filter((i) => !term || i.symbol.toUpperCase().includes(term))
      .filter((i) => !buyZone || i.timing === 'buy_zone')
      .filter((i) => !fewUnknown || (i.unknown_count ?? 99) <= 1)
      .filter((i) => minQ <= 0 || (i.scores.Q ?? -1) >= minQ)
      .filter((i) => withinPrice(i.last_close, price.min, price.max))
      .sort((a, b) => orderOf(a.class) - orderOf(b.class) || (b.checks_passed ?? -1) - (a.checks_passed ?? -1) || (b.scores.Q ?? -1) - (a.scores.Q ?? -1) || a.symbol.localeCompare(b.symbol));
  }, [items, klass, showExcluded, query, buyZone, fewUnknown, minQ, price]);

  useEffect(() => {                                              // keep the URL and local storage in step with the price filter
    try {
      const url = new URL(window.location.href);
      for (const [k, v] of [['price_min', price.min], ['price_max', price.max]] as const) { if (v === '') url.searchParams.delete(k); else url.searchParams.set(k, v); }
      window.history.replaceState(window.history.state, '', url);
      window.localStorage.setItem(PRICE_KEY, JSON.stringify(price));
    } catch { /* the filter still works without persistence */ }
  }, [price]);
  const priceActive = price.min !== '' || price.max !== '';
  const priceEmpty = priceActive && rows.length === 0 && items.length > 0 ? `No stocks ${price.max !== '' && price.min === '' ? `below ₹${price.max}` : `between ₹${price.min || '0'} and ₹${price.max || '∞'}`} in the current universe.` : null;

  const columns: Column<Item>[] = useMemo(() => [
    { key: 'symbol', header: 'Stock', sticky: true, sortValue: (i) => i.symbol, render: (i) => <><strong>{i.symbol}</strong>{i.kind && i.kind !== 'default' ? <small className="ui-muted"> {i.kind.replace(/_/g, ' ')}</small> : null}</> },
    { key: 'class', header: 'Class', sortValue: (i) => orderOf(i.class), render: (i) => <Chip status={i.class} title={LONG[i.class]} /> },
    ...(hasRank ? [{ key: 'rank', header: 'Rank', align: 'right' as const, sortValue: (i: Item) => i.rank, render: (i: Item) => i.rank ?? '—' }] : []),
    { key: 'price', header: 'Last close', align: 'right' as const, firstSort: 'asc', title: 'Latest stored daily close (₹)', sortValue: (i: Item) => (i.last_close == null ? null : Number(i.last_close)), render: (i: Item) => (i.last_close == null ? '—' : `₹${Number(i.last_close).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`) },
    ...SCORE_KEYS.map((k): Column<Item> => ({ key: k, header: k, firstSort: 'desc', title: { Q: 'Quality', G: 'Growth', V: 'Valuation', T: 'Timing', X: 'Cross-check' }[k], sortValue: (i) => i.scores[k], render: (i) => <ScoreBar value={i.scores[k]} label={k} naReason={NA_REASON} /> })),
    { key: 'checks', header: 'Checklist', sortValue: (i) => i.checks_passed, render: (i) => (i.checks_passed === null || i.checks_passed === undefined || !i.checks_total ? (i.checklist ?? '—') : <span title={i.checklist ?? ''}>{i.checks_passed}/{i.checks_total} <progress value={i.checks_passed} max={i.checks_total} aria-label="checks passed" /></span>) },
    { key: 'timing', header: 'Timing', sortValue: (i) => (i.timing === 'buy_zone' ? 0 : i.timing === 'wait_for_chart' ? 1 : 2), render: (i) => (i.timing === 'buy_zone' ? <Chip tone="positive">Buy zone</Chip> : i.timing === 'wait_for_chart' ? <Chip tone="caution">Wait for chart</Chip> : '—') },
    { key: 'unknown', header: 'Unknowns', align: 'right', sortValue: (i) => i.unknown_count, render: (i) => (i.unknown_count === undefined ? '—' : i.unknown_count === 0 ? '0' : <Chip tone="caution" title={(i.unknown_ids ?? []).map(nice).join(', ')}>{i.unknown_count} unknown</Chip>) },
  ], [hasRank]);

  const classTabs = [{ id: 'all', label: 'All', count: items.filter((i) => showExcluded || !EXCLUDED.has(i.class)).length },
    ...CLASS_ORDER.filter((c) => (showExcluded || !EXCLUDED.has(c)) && (counts[c] ?? 0) > 0).map((c) => ({ id: c, label: toneLabel(c), count: counts[c] ?? 0 }))];

  const summary = `${counts.multibagger_candidate ?? 0} candidates · ${counts.quality_compounder ?? 0} compounders · ${counts.emerging ?? 0} emerging · ${counts.watch ?? 0} watch.`
    + (gaps.length ? ` ${list(gaps.map(nice)).replace(/^./, (c) => c.toUpperCase())} data ${gaps.length === 1 ? 'is' : 'are'} not stored for most stocks${(counts.multibagger_candidate ?? 0) === 0 ? ', so unknown checks keep every stock below candidate until verified' : ''}.` : '');

  return (
    <section aria-label="Multibagger screen" className="mb-page">
      <header className="mb-head"><h3>Multibagger screen {data?.shadow && <Chip tone="caution" title="Computed every night but used by no card or verdict">Shadow</Chip>}</h3>
        <span className="ui-muted">long-term only · {data?.model_version ?? '…'}{data?.as_of ? ` · as of ${formatIST(data.as_of).replace(/, \d\d:\d\d IST$/, '')}` : ''}</span></header>
      {data?.shadow && !dismissed && <Banner tone="caution" onDismiss={() => { setDismissed(true); try { window.sessionStorage.setItem(DISMISS_KEY, '1'); } catch { /* per-session only */ } }}>Shadow mode: these results are computed every night and shown here for you to read, but no card, verdict or buy decision uses them yet.</Banner>}
      {data && <p className="mb-summary" role="status">{summary}</p>}
      {data?.universe_rule && <p className="ui-muted">{data.universe_rule}</p>}
      {data?.message && items.length === 0 && <EmptyState>{data.message}</EmptyState>}

      <Tabs label="Class filter" items={classTabs} active={klass} onChange={setKlass} />
      <div className="mb-filters">
        <input type="search" placeholder="Search symbol" value={query} onChange={(e) => setQuery(e.target.value)} aria-label="Search symbol" />
        <label><input type="checkbox" checked={buyZone} onChange={(e) => setBuyZone(e.target.checked)} /> Buy zone</label>
        <label><input type="checkbox" checked={fewUnknown} onChange={(e) => setFewUnknown(e.target.checked)} /> 0–1 unknown checks</label>
        <label>Min Q <input type="number" min={0} max={100} value={minQ} onChange={(e) => setMinQ(Number(e.target.value) || 0)} aria-label="Minimum Q score" /></label>
        <label><input type="checkbox" checked={showExcluded} onChange={(e) => { setShowExcluded(e.target.checked); if (!e.target.checked && EXCLUDED.has(klass)) setKlass('all'); }} /> Show excluded ({(counts.avoid ?? 0) + (counts.not_tradable ?? 0) + (counts.not_assessable ?? 0)})</label>
        <span className="mb-price" role="group" aria-label="Price filter">
          <select aria-label="Price preset" value={PRICE_PRESETS.some((p) => p.max === price.max && price.min === '') ? price.max : 'custom'} onChange={(e) => setPrice({ min: '', max: e.target.value === 'custom' ? price.max : e.target.value })}>
            {PRICE_PRESETS.map((p) => <option key={p.label} value={p.max}>{p.label}</option>)}
            <option value="custom">Custom…</option>
          </select>
          <input type="number" min={0} placeholder="Min ₹" value={price.min} onChange={(e) => setPrice({ ...price, min: e.target.value })} aria-label="Minimum price" />
          <input type="number" min={0} placeholder="Max ₹" value={price.max} onChange={(e) => setPrice({ ...price, max: e.target.value })} aria-label="Maximum price" />
        </span>
        <span className="ui-muted" role="status">{rows.length} of {items.length} stocks</span>
      </div>
      {priceActive && <p className="ui-muted">Price is the latest stored daily close. Low-price stocks are often illiquid: the tradability gates below are shown unchanged, and a stock with no stored close is left out of a price filter.</p>}

      <p className="ui-muted mb-legend">Scores out of 100: Q quality, G growth, V valuation, T timing, X cross-check. Select a row for its full checklist.</p>
      <DataTable caption="Multibagger screen" columns={columns} rows={rows} rowKey={(i) => i.instrument_id} virtualiseAbove={40} loading={!data && !error} error={error} onRetry={load}
        selectedKey={items.find((i) => i.symbol === selected)?.instrument_id ?? null} onRowClick={(i) => setSelected(i.symbol)}
        empty={<>{priceEmpty ?? 'No stocks match these filters.'} <Button variant="ghost" size="sm" onClick={() => { setKlass('all'); setQuery(''); setBuyZone(false); setFewUnknown(false); setMinQ(0); setPrice({ min: '', max: '' }); }}>Clear filters</Button></>} />
      <p className="ui-muted">{data?.disclaimer} A score exists only when 70% of its inputs are stored; unknown is shown as n/a and never as zero.</p>
      <Sheet open={selected !== null} onClose={() => setSelected(null)} title={selected ?? ''}>{selected && <StockDetail symbol={selected} onOpen={onOpen} />}</Sheet>
    </section>
  );
}

function toneLabel(c: string): string { return ({ multibagger_candidate: 'Candidates', quality_compounder: 'Compounders', emerging: 'Emerging', new_listing: 'New listing', watch: 'Watch', not_a_fit: 'Not a fit', not_tradable: 'Not tradable', avoid: 'Avoid', not_assessable: 'Not assessable' } as Record<string, string>)[c] ?? c; }

function StockDetail({ symbol, onOpen }: { symbol: string; onOpen?: (symbol: string) => void }) {
  const [d, setD] = useState<Detail | null>(null);
  const [error, setError] = useState<unknown>(null);
  const fetchDetail = useCallback(() => { setError(null); setD(null); apiRequest<Detail>(`/research/multibagger/${encodeURIComponent(symbol)}`).then(setD).catch((e: unknown) => setError(e)); }, [symbol]);
  useEffect(() => { fetchDetail(); }, [fetchDetail]);
  if (error) return <EmptyState action={<Button size="sm" onClick={fetchDetail}>Retry</Button>}>{error instanceof Error ? error.message : 'The detail could not be loaded.'}</EmptyState>;
  if (!d) return <p role="status" aria-busy="true">Loading…</p>;
  const group = (r: Check['result']) => d.checks.filter((c) => c.result === r);
  const unknown = group('unknown');
  return (
    <>
      <div className="ui-row"><Chip status={d.class} />{d.timing === 'buy_zone' && <Chip tone="positive">Buy zone</Chip>}<span className="ui-muted">{d.checklist}</span></div>
      <p>{LONG[d.class] ?? d.class}</p>
      {d.reasons.length > 0 && <ul>{d.reasons.slice(0, 3).map((r) => <li key={r}>{r}</li>)}</ul>}
      <div className="mb-scores">{SCORE_KEYS.map((k) => <div key={k}><span className="ui-muted">{{ Q: 'Quality', G: 'Growth', V: 'Valuation', T: 'Timing', X: 'Cross-check' }[k]}</span><ScoreBar value={d.scores[k]} label={k} naReason={NA_REASON} /></div>)}</div>
      {(['fail', 'unknown', 'pass'] as const).map((r) => group(r).length > 0 && (
        <details key={r} open={r !== 'pass'}><summary>{{ fail: 'Failed', unknown: 'Unknown', pass: 'Passed' }[r]} ({group(r).length})</summary>
          <ul className="mb-checks">{group(r).map((c) => <li key={c.id}><strong>{c.id.replace(/_/g, ' ')}</strong> <span className="ui-muted">{c.reason}</span></li>)}</ul></details>
      ))}
      {unknown.length > 0 && (
        <div><h4>Verify</h4><p className="ui-muted">Storing these would change the class{d.class_if_unknown_checks_cleared?.class && d.class_if_unknown_checks_cleared.class !== d.class ? ` (it could reach ${toneLabel(d.class_if_unknown_checks_cleared.class)})` : ''}:</p>
          <ul>{unknown.map((c) => <li key={c.id}>{c.id.replace(/_/g, ' ')}</li>)}</ul></div>
      )}
      {onOpen && <Button variant="primary" onClick={() => onOpen(symbol)}>Open research</Button>}
    </>
  );
}
