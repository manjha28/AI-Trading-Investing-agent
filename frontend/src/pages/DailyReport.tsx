import { useCallback, useEffect, useMemo, useState } from 'react';
import { apiRequest } from '../api';
import type { Instrument } from './researchTypes';
import { formatIST } from '../lib/formatIST';
import { tickPrice } from '../lib/formatPrice';
import { cardState } from '../lib/cardState';

export type Levels = {
  status: string; reason?: string; entry_status?: string; selected_setup?: string; breakout_trigger?: string | null; stop_reason?: string; stop_method?: string; explanation?: string; direction?: string; technical_score?: number; market_regime?: string; risk_reward?: Record<string, number>;
  entry_zone?: { low: string; high: string }; stop?: string; reference_close?: string; prior_high_reference?: string; entry_label?: string; ready_to_buy?: boolean; r_definition?: string; watch_band?: { low: string; high: string; label: string } | null; event_gate?: string; gates?: { event_calendar_note?: string; liquidity?: { reason: string } };
  pivot_support?: { name: string; price: string } | null; pivot_resistance?: { name: string; price: string } | null; pivots?: Record<string, Record<string, string>> | null;
  targets?: { label: string; price: string; risk_reward?: number; basis?: string }[]; derivation?: { rule: string; inputs: Record<string, number>; tick: string };
};
export type WinConfidence = { line: string; band?: string | null; band_label?: string | null; tooltip?: string | null; expected_r?: number | null };
export type Confidence = { score: number; win_confidence?: WinConfidence | null; band: string; kind?: 'factor_v2'; label?: string; grade?: string | null; measured_line?: string | null; factor_rank?: { rank: number; of: number; composite: number; percentile: number; as_of?: string; universe_size?: number; text: string } | null; setup?: { label: string; checks: { key: string; label: string; result: string; reason: string; veto: boolean }[]; failed_vetoes: string[] }; note: string; parts: { key: string; label: string; value: number; weight: number }[]; horizon?: string };
type HorizonRec = { recommendation: string | null; score: number | null; confidence: number | null; buy_confidence?: Confidence | null; why_not: string[]; data_note?: string | null };
type Bucket = 'buy' | 'setup_waiting' | 'radar' | 'hold' | 'no_new_entry' | 'stale_data' | 'not_enough_data';
export type PriceBlock = { value: string; kind: 'ltp' | 'close'; as_of: string | null; change: string | null; change_pct: string | null; stale: boolean };
export type Row = {
  price?: PriceBlock | null;
  instrument_id: string; symbol: string; name: string; held: boolean; quantity: string | null; bucket: Bucket; action: string; researched_at: string | null; stale: boolean;
  horizons: Record<string, HorizonRec>; levels: Record<string, Levels | null>; last_close?: string | null; confidence?: Confidence | null; researching?: boolean; data_note?: string | null; radar_reasons?: string[] | null;
};
type Coverage = { universe: number; researched_current: number; stale: number; never_researched: number; last_research_at: string | null; universe_rule: string; schedule: string };
type Report = { coverage?: Coverage; as_of: string; buckets: Record<Bucket, Row[]>; counts: Record<Bucket, number>; scope: string; disclaimer: string; researching?: string[] };

const HORIZONS = ['swing', 'positional', 'long_term'] as const;
const HORIZON_NAME: Record<string, string> = { swing: 'Swing', positional: 'Positional', long_term: 'Long-term' };
const HORIZON_SPAN: Record<string, string> = { swing: 'days to weeks', positional: 'weeks to months', long_term: 'months to years' };
const TABS: { key: Bucket | 'all'; label: string }[] = [
  { key: 'all', label: 'All' }, { key: 'buy', label: 'Ready to buy' }, { key: 'setup_waiting', label: 'Set up, waiting' }, { key: 'radar', label: 'Radar' }, { key: 'hold', label: 'Hold' }, { key: 'no_new_entry', label: 'No new entry' }, { key: 'stale_data', label: 'Not current' }, { key: 'not_enough_data', label: 'Needs data' },
];
const REC_TEXT: Record<string, string> = { BUY: 'Buy', HOLD: 'Hold', AVOID: 'Avoid', WAIT: 'Wait', INSUFFICIENT_EVIDENCE: 'No data' };
const POLL_FAST_MS = 15_000;
const POLL_SLOW_MS = 300_000;

export const inr = (v?: string | number | null) => (v == null ? '—' : `₹${tickPrice(v)}`);
export const when = (iso: string | null) => formatIST(iso, 'not yet');
const bandClass = (band?: string) => (band === 'High' ? 'good' : band === 'Medium' ? 'mid' : 'bad');

/** The calibration band is shown only when the stored validation says the buckets separate; otherwise the number stands alone, marked Unproven. */
export function shownBand(conf: Confidence): string | null {
  const w = conf.win_confidence;
  return w?.band_label && !/not yet separating|unproven/i.test(w.band_label) ? (w.band ?? w.band_label) : null;
}

/** "Win rate 50% · 118 past setups": the one short evidence chip; the full sentence lives in the Why fold. */
export function evidenceChip(conf: Confidence | null | undefined): { text: string; title: string | undefined } | null {
  const w = conf?.win_confidence;
  if (!w?.line) return null;
  const pct = /win rate\s+(\d+(?:\.\d+)?)%/i.exec(w.line)?.[1]; const n = /based on\s+(\d+)/i.exec(w.line)?.[1];
  return { text: pct && n ? `Win rate ${pct}% · ${n} past setups` : w.line.slice(0, 40), title: w.tooltip ?? undefined };
}

export function WinLine({ conf }: { conf: Confidence }) {          // the full sentence, shown inside the Why fold
  const w = conf.win_confidence;
  if (!w?.line) return null;
  return <p className="rt-note rt-measured" title={w.tooltip ?? undefined}>{w.line}{w.band_label ? ` · ${w.band_label}` : ''}{w.expected_r != null ? ` · expected ${w.expected_r > 0 ? '+' : ''}${w.expected_r.toFixed(2)}R` : ''}</p>;
}

export function RankBadge({ conf }: { conf: Confidence }) {      // Nexus v2: a neutral factor rank plus the setup grade; no confidence wording, no colour scale
  return <div className="rt-rankbadge" role="img" aria-label={`${conf.factor_rank?.text ?? 'Factor rank unavailable'}; ${conf.setup?.label ?? ''}`}>
    <b>{conf.label === 'High' ? 'High' : conf.setup?.label ?? 'Not ready'}</b><small>{conf.factor_rank?.text ?? 'Factor rank unavailable'}</small>
  </div>;
}

export function Ring({ score, band }: { score: number; band: string | null }) {
  const r = 26; const c = 2 * Math.PI * r;
  return <div className={`rt-ring rt-ring-${band ? bandClass(band) : 'neutral'}`} role="img" aria-label={`Score ${Math.round(score)} out of 100, ${band ?? 'unproven'}`}>
    <div className="rt-ring-dial">
      <svg viewBox="0 0 64 64" aria-hidden="true"><circle cx="32" cy="32" r={r} className="rt-ring-bg" /><circle cx="32" cy="32" r={r} className="rt-ring-fg" strokeDasharray={`${(score / 100) * c} ${c}`} transform="rotate(-90 32 32)" /></svg>
      <b>{Math.round(score)}</b>
    </div>
    <small>{band ?? 'Unproven'}</small>
  </div>;
}

function PriceLine({ price }: { price: PriceBlock | null | undefined }) {
  if (!price) return <div className="rt-priceline"><b className="rt-price rt-price-none" title="No stored price for this stock">—</b></div>;
  const ch = Number(price.change); const known = price.change !== null && Number.isFinite(ch);
  const asOf = price.as_of ? (price.kind === 'ltp' ? `as of ${formatIST(price.as_of).replace(/^.*?,\s*/, '')}` : `close ${new Date(price.as_of).toLocaleDateString('en-IN', { day: 'numeric', month: 'short', timeZone: 'Asia/Kolkata' })}`) : (price.kind === 'ltp' ? 'live' : 'last close');
  return <div className={`rt-priceline${price.stale ? ' stale' : ''}`}>
    <b className="rt-price" title={price.kind === 'ltp' ? 'Live price' : 'Last close'}>{inr(price.value)}</b>
    {known && <span className={`rt-change ${ch >= 0 ? 'rt-pos' : 'rt-neg'}`}>{ch >= 0 ? '+' : '−'}{inr(String(Math.abs(ch)))}{price.change_pct ? ` (${Number(price.change_pct) >= 0 ? '+' : '−'}${Math.abs(Number(price.change_pct)).toFixed(2)}%)` : ''}</span>}
    <small className="rt-asof">{asOf}{price.stale ? ' · stale' : ''}</small>
  </div>;
}

/** Horizontal price ladder: stop, entry zone, last close and targets drawn to scale. */
export function Ladder({ levels, lastClose }: { levels: Levels; lastClose?: string | null }) {
  const stop = Number(levels.stop); const lo = Number(levels.entry_zone?.low); const hi = Number(levels.entry_zone?.high); const close = Number(levels.reference_close);
  const targets = (levels.targets ?? []).map((t) => ({ ...t, v: Number(t.price) }));
  const max = Math.max(...targets.map((t) => t.v), hi); const min = stop; const span = Math.max(max - min, 1e-9);
  const pos = (v: number) => `${Math.min(100, Math.max(0, ((v - min) / span) * 100))}%`;
  return <div className="rt-ladder" role="img" aria-label={`Stop ${inr(stop)}, entry ${inr(lo)} to ${inr(hi)}, targets ${targets.map((t) => inr(t.v)).join(', ')}`}>
    <div className="rt-ladder-track">
      <div className="rt-ladder-risk" style={{ left: '0%', width: pos(lo) }} />
      <div className="rt-ladder-zone" style={{ left: pos(lo), width: `calc(${pos(hi)} - ${pos(lo)})` }} />
      <div className="rt-ladder-reward" style={{ left: pos(hi), width: `calc(100% - ${pos(hi)})` }} />
      <i className="rt-pin rt-pin-stop" style={{ left: pos(stop) }} /><i className="rt-pin rt-pin-close" style={{ left: pos(close) }} />
      {targets.map((t) => <i key={t.label} className="rt-pin rt-pin-target" style={{ left: pos(t.v) }} />)}
    </div>
    <ul className="rt-ladder-keys">
      <li className="bad"><small>Stop</small><b>{inr(levels.stop)}</b></li>
      <li className="mid"><small>{cardState(levels, lastClose).kind === 'in_band' ? 'In the band: limit between' : 'Wait, then limit between'}</small><b className="rt-range"><span>{inr(levels.entry_zone?.low)}</span><span>– {inr(levels.entry_zone?.high)}</span></b></li>
      {[0, 1, 2].map((n) => { const t = levels.targets?.[n]; return <li key={n} className="good" title={t?.basis}><small>Target {t?.label ?? `T${n + 1}`}{t?.risk_reward ? ` · ${t.risk_reward}R` : ''}</small><b>{inr(t?.price)}</b></li>; })}
    </ul>
  </div>;
}

/** The number a Ready-to-buy card shows when it opens: the positional horizon if it is a BUY, otherwise the first BUY horizon. The list is sorted by exactly this number. */
export function displayedHorizon(row: Row): string | undefined {
  const buys = HORIZONS.filter((h) => row.horizons[h]?.recommendation === 'BUY');
  return buys.includes('positional' as never) ? 'positional' : buys[0];
}
export function displayedScore(row: Row): number {
  const h = displayedHorizon(row);
  const c = (h ? row.horizons[h]?.buy_confidence : null) ?? row.confidence;
  return c ? c.score : -1;
}
export function sortByDisplayedScore(rows: Row[]): Row[] {
  return [...rows].sort((a, b) => displayedScore(b) - displayedScore(a) || (b.confidence?.score ?? 0) - (a.confidence?.score ?? 0) || a.symbol.localeCompare(b.symbol));
}

function BuyCard({ row, onOpen, showAction = false }: { row: Row; onOpen: (row: Row) => void; showAction?: boolean }) {
  const buyHorizons = HORIZONS.filter((h) => row.horizons[h]?.recommendation === 'BUY');
  const [horizon, setHorizon] = useState<string>(buyHorizons.includes('positional' as never) ? 'positional' : buyHorizons[0] ?? 'positional');
  const active = HORIZONS.includes(horizon as never) ? horizon : buyHorizons[0];                // any horizon can be opened: a Hold or Avoid horizon explains itself instead of being a dead button
  const conf = row.horizons[active]?.buy_confidence ?? row.confidence;
  const levels = row.levels[active];
  const evid = evidenceChip(conf);
  return <article className={`rt-buy${showAction ? ` rt-buy-${row.bucket}` : ''}`}>
    <header className="rt-head">
      <div className="rt-head-id"><button type="button" className="rt-sym" onClick={() => onOpen(row)} title={row.symbol}>{row.symbol}</button><span className="rt-name" title={row.name}>{row.name}</span></div>
      {conf && (conf.kind === 'factor_v2' ? <RankBadge conf={conf} /> : <Ring score={conf.score} band={shownBand(conf)} />)}
    </header>
    <PriceLine price={row.price} />
    <div className="rt-chips">{evid && <span className="rt-chip rt-chip-evidence" title={evid.title}>{evid.text}</span>}{row.held && <span className="rt-chip rt-chip-held" title={`You hold ${row.quantity}`}>In portfolio</span>}{row.stale && <span className="rt-chip rt-chip-warn">Not current</span>}</div>
    <div className="rt-seg" role="tablist" aria-label={`${row.symbol} horizon`}>
      {HORIZONS.map((h) => {
        const rec = row.horizons[h]?.recommendation;
        const on = rec === 'BUY';
        return <button key={h} type="button" role="tab" aria-selected={active === h} className={active === h ? 'active' : ''} onClick={() => setHorizon(h)} title={on ? HORIZON_SPAN[h] : `${REC_TEXT[rec ?? ''] ?? '—'} on this horizon`}>
          {HORIZON_NAME[h]}<small>{on ? 'Buy' : REC_TEXT[rec ?? ''] ?? '—'}</small></button>;
      })}
    </div>
    <div className="rt-slot-status">{showAction && levels?.status !== 'available' && <p className="rt-setup rt-action" title={[row.action, ...(row.radar_reasons ?? [])].join(' · ')}>{row.action}{row.radar_reasons?.[0] ? ` · ${row.radar_reasons[0]}` : ''}</p>}{levels?.status === 'available' && <p className="rt-setup"><span className={`rt-chip ${cardState(levels, row.last_close).kind === 'in_band' ? 'rt-chip-ok' : 'rt-chip-warn'}`} title={cardState(levels, row.last_close).reason ?? cardState(levels, row.last_close).label}>{cardState(levels, row.last_close).compact}</span> <span className="rt-chip">{(levels.selected_setup ?? '').replace(/_/g, ' ').toLowerCase()}</span>{levels.breakout_trigger && <span className="rt-chip">trigger {inr(levels.breakout_trigger)}</span>}</p>}</div>
    <div className="rt-slot-ladder">{levels?.status === 'available' ? <Ladder levels={levels} lastClose={row.last_close} /> : row.horizons[active]?.recommendation !== 'BUY'
      ? <p className="rt-note"><b>{HORIZON_NAME[active]}: {REC_TEXT[row.horizons[active]?.recommendation ?? ''] ?? 'no call'}.</b> {[...(row.horizons[active]?.why_not ?? []), row.horizons[active]?.data_note].filter(Boolean).join(' · ') || 'No buy signal on this horizon, so no levels are drawn.'}</p>
      : <p className="rt-note">{levels?.reason ?? 'No levels for this horizon.'}</p>}</div>
    <div className="rt-slot-notes">{levels?.status === 'available' && <p className="rt-note">Last close {inr(levels.reference_close)}{levels.prior_high_reference ? ` · prior high ${inr(levels.prior_high_reference)}` : ''} · {HORIZON_SPAN[active]}</p>}
    {levels?.status === 'available' && levels.event_gate && <p className="rt-note"><b>Event:</b> {levels.event_gate}</p>}
    {levels?.status === 'available' && levels.gates?.event_calendar_note?.startsWith('EVENT CALENDAR UNRELIABLE') && <p className="rt-note">{levels.gates.event_calendar_note}</p>}
    {levels?.status === 'available' && (levels.pivot_support || levels.pivot_resistance) && <p className="rt-note">Pivot support {levels.pivot_support ? `${inr(levels.pivot_support.price)} (${levels.pivot_support.name})` : 'none below price'} · pivot resistance {levels.pivot_resistance ? `${inr(levels.pivot_resistance.price)} (${levels.pivot_resistance.name})` : 'none above price'}</p>}</div>
    {conf && <details className="rt-fold rt-why"><summary>{conf.kind === 'factor_v2' ? 'Why this rank and grade?' : `Why ${Math.round(conf.score)}/100?`}</summary>
      <WinLine conf={conf} />
      {conf.measured_line && <p className="rt-note rt-measured" title="Hypothetical past results of similar setups on a survivor-free replay; not a forecast">{conf.measured_line}</p>}
      <ul className="rt-parts">{(conf.parts ?? []).map((p) => <li key={p.key}><span>{p.label}<small> · {Math.round(p.weight * 100)}% weight</small></span><div className="rt-bar"><i style={{ width: `${p.value}%` }} /></div><b>{Math.round(p.value)}</b></li>)}</ul>
      {conf.kind === 'factor_v2' && conf.setup && <ul className="rt-checks">{(conf.setup.checks ?? []).map((c) => <li key={c.key}><b>{c.result === 'pass' ? 'Pass' : c.result === 'not_evaluated' ? 'Not evaluated' : c.veto ? 'Fail (veto)' : 'Fail'}</b> {c.label}<small> · {c.reason}</small></li>)}</ul>}
      <p className="rt-note">{conf.note}{conf.kind === 'factor_v2' ? '' : ' Provisional ranking of conviction, not a probability of profit.'}</p>
      {levels?.explanation && <p className="rt-note">{levels.explanation}</p>}{levels?.derivation && <p className="rt-note"><b>Levels:</b> {levels.derivation.rule}</p>}</details>}
    {!conf && <div className="rt-why rt-why-empty" aria-hidden="true" />}
    <footer><small>Researched {when(row.researched_at)}</small><button type="button" className="rt-btn rt-ghost" onClick={() => onOpen(row)}>Full research →</button></footer>
  </article>;
}

/** One card per stock, as before. The list payload is compact, so the long derivations and the "Why" parts of THIS card are fetched when its "Why" section is opened. */
function LazyBuyCard({ row, onOpen, showAction = false }: { row: Row; onOpen: (row: Row) => void; showAction?: boolean }) {
  const [full, setFull] = useState<Row | null>(null);
  const load = () => {
    if (full) return;
    void apiRequest<Row>(`/research/daily-report/${row.instrument_id}`).then((r) => { if (r && r.instrument_id === row.instrument_id) setFull(r); }).catch(() => undefined);
  };
  return <div onClickCapture={(e) => { if ((e.target as HTMLElement).closest('summary')) load(); }}><BuyCard row={full ? { ...row, ...full } : row} onOpen={onOpen} showAction={showAction} /></div>;
}

function CompactRow({ row, onOpen }: { row: Row; onOpen: (row: Row) => void }) {
  return <tr className={row.researching ? 'rt-skel' : ''}>
    <td><button type="button" className="rt-sym" onClick={() => onOpen(row)}>{row.symbol}</button><small className="rt-name">{row.name}</small></td>
    <td>{row.held ? <span className="rt-chip rt-chip-held">Holding {row.quantity ? Number(row.quantity).toLocaleString('en-IN') : ''}</span> : <span className="rt-muted">—</span>}</td>
    <td className="rt-act">{row.researching ? <span className="rt-spin">Researching…</span> : row.action}{row.radar_reasons?.map((r) => <small key={r} className="rt-datanote">{r}</small>)}{HORIZONS.filter((h) => row.horizons[h]?.data_note).slice(0, 2).map((h) => <small key={`note-${h}`} className="rt-datanote">{HORIZON_NAME[h]}: {row.horizons[h].data_note}</small>)}{row.data_note && <small className="rt-datanote">{row.data_note}</small>}{row.stale && !row.data_note && !row.researching && row.researched_at && <small className="rt-datanote">Last researched {when(row.researched_at)}; a refresh is queued for tonight's run.</small>}</td>
    <td className="rt-dots">{HORIZONS.map((h) => { const rec = row.horizons[h]?.recommendation; return <span key={h} className={`rt-dot rt-dot-${rec ?? 'none'}`} title={`${HORIZON_NAME[h]}: ${row.horizons[h]?.data_note ?? REC_TEXT[rec ?? ''] ?? '—'}`}>{HORIZON_NAME[h][0]}</span>; })}</td>
    <td className="r">{inr(row.last_close)}</td>
    <td className="rt-muted">{when(row.researched_at)}</td>
  </tr>;
}

const V2_KEY = 'research_sections_v2';
/** Flag research_sections_v2: default OFF. Switch it with the toggle on the report or ?research_sections_v2=1 / =0 in the URL (kept in local storage). */
function useSectionsV2(): [boolean, (on: boolean) => void] {
  const [on, setOn] = useState(() => {
    try {
      const q = new URLSearchParams(window.location.search).get(V2_KEY);
      if (q === '1' || q === '0') { window.localStorage.setItem(V2_KEY, q); return q === '1'; }
      return window.localStorage.getItem(V2_KEY) === '1';
    } catch { return false; }
  });
  return [on, (v) => { setOn(v); try { window.localStorage.setItem(V2_KEY, v ? '1' : '0'); } catch { /* per browser only */ } }];
}

type SortMode = 'score' | 'symbol' | 'held';
const sortRows = (rows: Row[], mode: SortMode) => (mode === 'symbol' ? [...rows].sort((a, b) => a.symbol.localeCompare(b.symbol)) : mode === 'held' ? [...rows].sort((a, b) => Number(b.held) - Number(a.held) || a.symbol.localeCompare(b.symbol)) : sortByDisplayedScore(rows));

/** Ready to buy, Hold and Radar in one card style (the same card as the buy cards, fields that do not apply shown as a dash), each section with its count, plus one search and one sort. */
function SectionsV2({ buckets, onOpen }: { buckets: { buy: Row[]; hold: Row[]; radar: Row[] }; onOpen: (row: Row) => void }) {
  const [query, setQuery] = useState('');
  const [sort, setSort] = useState<SortMode>('score');
  const term = query.trim().toUpperCase();
  const match = (r: Row) => !term || r.symbol.toUpperCase().includes(term) || r.name.toUpperCase().includes(term);
  const parts: { key: 'buy' | 'hold' | 'radar'; title: string; hint: string }[] = [
    { key: 'buy', title: 'Ready to buy', hint: 'a buy signal with a range' }, { key: 'hold', title: 'Hold', hint: 'in your portfolio: keep holding' }, { key: 'radar', title: 'Radar', hint: 'buy signal, no range yet' },
  ];
  return <div className="rt-v2" data-testid="sections-v2">
    <div className="rt-v2-bar"><input type="search" placeholder="Search symbol or name" aria-label="Search sections" value={query} onChange={(e) => setQuery(e.target.value)} />
      <label>Sort <select aria-label="Sort sections" value={sort} onChange={(e) => setSort(e.target.value as SortMode)}><option value="score">Score</option><option value="symbol">Symbol</option><option value="held">Held first</option></select></label></div>
    {parts.map(({ key, title, hint }) => {
      const all = buckets[key]; const shown = sortRows(all.filter(match), sort);
      return <section key={key} aria-label={title}><h3 className="rt-h">{title} <b className="rt-count">{shown.length}{term && shown.length !== all.length ? ` of ${all.length}` : ''}</b> <small>{hint}</small></h3>
        {shown.length === 0 ? <p className="rt-note">{all.length === 0 ? `Nothing in ${title} right now.` : 'No match for this search.'}</p> : <div className="rt-buy-grid">{shown.map((r) => <LazyBuyCard key={r.instrument_id} row={r} onOpen={onOpen} showAction />)}</div>}</section>;
    })}
  </div>;
}

/** Daily research report: swing, positional and long-term only. Refreshes itself; new stocks appear without a reload. */
export function DailyReport({ onOpen }: { onOpen: (instrument: Instrument) => void }) {
  const [report, setReport] = useState<Report | null>(null);
  const [error, setError] = useState('');
  const [tab, setTab] = useState<Bucket | 'all'>('all');
  const [limit, setLimit] = useState(25);
  const [v2, setV2] = useSectionsV2();
  const load = useCallback(() => apiRequest<Report>('/research/daily-report').then((r) => { setReport(r); setError(''); }).catch(() => setError('The daily report could not be loaded. Retrying automatically.')), []);
  const researching = (report?.researching?.length ?? 0) > 0;
  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), researching ? POLL_FAST_MS : POLL_SLOW_MS);
    const onFocus = () => void load();
    window.addEventListener('focus', onFocus);
    return () => { window.clearInterval(timer); window.removeEventListener('focus', onFocus); };
  }, [load, researching]);
  const open = (row: Row) => onOpen({ id: row.instrument_id, symbol: row.symbol, display_name: row.name, venue: 'NSE', is_synthetic: false, card_researched_at: row.researched_at });
  const rows = useMemo(() => (report ? (tab === 'all' ? (Object.values(report.buckets).flat() as Row[]) : (report.buckets[tab] ?? [])) : []), [report, tab]);
  if (!report) return <section className="rt-card rt-daily" aria-label="Daily report">{error ? <p className="rt-alert rt-alert-error" role="alert">{error}</p> : <div className="rt-skel-block" aria-busy="true">Loading the daily report…</div>}</section>;
  const total = Object.values(report.counts).reduce((a, b) => a + b, 0);
  const buys = tab === 'all' || tab === 'buy' ? report.buckets.buy : [];
  const waiting = tab === 'all' || tab === 'setup_waiting' ? (report.buckets.setup_waiting ?? []) : [];
  const hasWaiting = report.counts.setup_waiting !== undefined;
  const nonBuy = rows.filter((r) => r.bucket !== 'buy' && r.bucket !== 'setup_waiting' && !(v2 && (r.bucket === 'hold' || r.bucket === 'radar')));
  const needs = nonBuy.filter((r) => r.bucket === 'not_enough_data');          // collapsed by default: the reason is on each row
  const allOthers = tab === 'not_enough_data' ? nonBuy : nonBuy.filter((r) => r.bucket !== 'not_enough_data');
  const others = allOthers.slice(0, limit);
  const tiles: { key: Bucket; label: string; hint: string }[] = [
    { key: 'buy', label: 'Ready to buy', hint: hasWaiting ? 'in the band now' : 'with a range' }, ...(hasWaiting ? [{ key: 'setup_waiting' as Bucket, label: 'Set up, waiting', hint: 'levels set, price not in band' }] : []), { key: 'radar', label: 'Radar', hint: 'buy signal, no range yet' }, { key: 'hold', label: 'Hold', hint: 'in your portfolio' },
    { key: 'no_new_entry', label: 'No new entry', hint: 'not a buy now' }, { key: 'stale_data', label: 'Not current', hint: 'close or research is old' }, { key: 'not_enough_data', label: 'Needs data', hint: 'too little stored data' },
  ];
  return <section className="rt-daily" aria-label="Daily report">
    <div className="rt-daily-head">
      <div><h2>Today&apos;s research</h2><p className="rt-note">Swing, positional and long-term · {report.coverage?.universe_rule ?? `${total} stocks`} · updated {when(report.as_of)}</p></div>
      <label className="rt-v2-toggle" title="Preview: Ready to buy, Hold and Radar as one card style with search and sort"><input type="checkbox" checked={v2} onChange={(e) => setV2(e.target.checked)} /> New sections layout (preview)</label>
      <span className={`rt-live${researching ? ' busy' : ''}`}>{researching ? `Researching ${report.researching?.join(', ')}…` : 'Auto-updating'}</span>
    </div>
    {error && <p className="rt-alert rt-alert-error" role="alert">{error}</p>}
    {report.coverage && <details className="rt-cover"><summary><b>Coverage</b> · {report.coverage.researched_current} of {report.coverage.universe} stocks researched and current{report.coverage.stale > 0 ? ` · ${report.coverage.stale} not current` : ''}{report.coverage.never_researched > 0 ? ` · ${report.coverage.never_researched} waiting` : ''} · last run {when(report.coverage.last_research_at)}</summary>
      <p className="rt-note"><b>Universe:</b> {report.coverage.universe_rule}</p><p className="rt-note"><b>Schedule:</b> {report.coverage.schedule}</p>
      <p className="rt-note">Every stock in the universe is listed below, including any that failed or are still waiting. Nothing is hidden.</p></details>}
    <div className="rt-tiles">{tiles.map((t) => <button key={t.key} type="button" className={`rt-tile rt-tile-${t.key}${tab === t.key ? ' active' : ''}`} onClick={() => setTab(tab === t.key ? 'all' : t.key)}><b>{report.counts[t.key] ?? 0}</b><span>{t.label}</span><small>{t.hint}</small></button>)}</div>
    <div className="rt-seg rt-seg-wide" role="tablist" aria-label="Filter">{TABS.filter((t) => t.key !== 'setup_waiting' || hasWaiting).map((t) => <button key={t.key} type="button" role="tab" aria-selected={tab === t.key} className={tab === t.key ? 'active' : ''} onClick={() => setTab(t.key)}>{t.label}</button>)}</div>
    {total === 0 && <div className="rt-card rt-empty-state"><h3>Nothing to report yet</h3><p>Add a stock to a watchlist or record a holding. It is researched automatically and appears here within a minute or two.</p></div>}
    {v2 && (tab === 'all' || tab === 'buy' || tab === 'hold' || tab === 'radar') && <SectionsV2 buckets={{ buy: tab === 'all' || tab === 'buy' ? report.buckets.buy : [], hold: tab === 'all' || tab === 'hold' ? (report.buckets.hold ?? []) : [], radar: tab === 'all' || tab === 'radar' ? (report.buckets.radar ?? []) : [] }} onOpen={open} />}
    {!v2 && buys.length > 0 && <><h3 className="rt-h">Ready to buy <small>highest confidence first, by the number shown on each card</small></h3><div className="rt-buy-grid">{sortByDisplayedScore(buys).map((r) => <LazyBuyCard key={r.instrument_id} row={r} onOpen={open} />)}</div></>}
    {waiting.length > 0 && <><h3 className="rt-h">Set up, waiting <small>levels are set; price is not in the buy band yet</small></h3><div className="rt-buy-grid">{waiting.map((r) => <LazyBuyCard key={r.instrument_id} row={r} onOpen={open} />)}</div></>}
    {others.length > 0 && <div className="rt-card rt-compact"><div className="rt-table-wrap"><table className="rt-table"><thead><tr><th>Stock</th><th>Portfolio</th><th>What to do</th><th title="Swing / Positional / Long-term">S · P · L</th><th className="r">Last close</th><th>Researched</th></tr></thead>
      <tbody>{others.map((r) => <CompactRow key={r.instrument_id} row={r} onOpen={open} />)}</tbody></table></div>
      {allOthers.length > limit && <p className="rt-note"><button type="button" className="rt-btn rt-ghost" onClick={() => setLimit(limit + 100)}>Show more ({allOthers.length - limit} remaining)</button></p>}
      <p className="rt-note"><span className="rt-dot rt-dot-BUY">B</span> buy <span className="rt-dot rt-dot-HOLD">H</span> hold <span className="rt-dot rt-dot-WAIT">W</span> wait <span className="rt-dot rt-dot-AVOID">A</span> avoid <span className="rt-dot rt-dot-none">–</span> no data</p></div>}
    {tab !== 'not_enough_data' && needs.length > 0 && <details className="rt-card rt-compact rt-fold"><summary><b>Needs data ({needs.length})</b> · not enough stored evidence yet; the reason is on each row</summary><div className="rt-table-wrap"><table className="rt-table"><thead><tr><th>Stock</th><th>Portfolio</th><th>Why it needs data</th><th>S · P · L</th><th className="r">Last close</th><th>Researched</th></tr></thead><tbody>{needs.map((r) => <CompactRow key={r.instrument_id} row={r} onOpen={open} />)}</tbody></table></div></details>}
    <p className="rt-disclaimer">{report.disclaimer}</p>
  </section>;
}
