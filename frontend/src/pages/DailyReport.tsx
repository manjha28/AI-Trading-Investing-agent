import { useCallback, useEffect, useMemo, useState } from 'react';
import { apiRequest } from '../api';
import type { Instrument } from './researchTypes';
import { Chip, DataTable, type Column } from '../components/ui/kit';
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
  { key: 'all', label: 'All' }, { key: 'buy', label: 'Buy now' }, { key: 'setup_waiting', label: 'Wait for entry' }, { key: 'radar', label: 'Radar' }, { key: 'hold', label: 'Hold' }, { key: 'no_new_entry', label: 'No new entry' }, { key: 'stale_data', label: 'Not current' }, { key: 'not_enough_data', label: 'Needs data' },
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
  const stop = Number(levels.stop); const lo = Number(levels.entry_zone?.low); const hi = Number(levels.entry_zone?.high); const close = Number(lastClose ?? levels.reference_close);
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

/** The price a card is judged on: the price it shows (today's quote or the latest close), unless that is stale; then the close the research used. */
export const cardPrice = (row: Row): string | null | undefined => (row.price && !row.price.stale ? row.price.value : row.last_close);

/** "Buy now" means the price is inside the buy range of a BUY horizon; anything else waits. `gap` is how far the price is from the nearest range (a share of the price);
 *  Infinity when no range is waiting to be reached (for example the first target is already below the price). The positional horizon is preferred when it qualifies. */
export function entryFor(row: Row): { horizon: string | undefined; inRange: boolean; gap: number } {
  const buys = HORIZONS.filter((h) => row.horizons[h]?.recommendation === 'BUY').sort((a, b) => Number(b === 'positional') - Number(a === 'positional'));
  const price = cardPrice(row);
  const states = buys.map((h) => ({ h, kind: cardState(row.levels[h], price).kind }));
  const inside = states.find((s) => s.kind === 'in_band');
  if (inside) return { horizon: inside.h, inRange: true, gap: 0 };
  const waits = states.filter((s) => s.kind === 'above_band' || s.kind === 'below_band').map((s) => {
    const zone = row.levels[s.h]?.entry_zone; const p = Number(price ?? row.levels[s.h]?.reference_close);
    return { h: s.h, gap: Math.abs(p - Number(s.kind === 'above_band' ? zone?.high : zone?.low)) / p };
  }).sort((a, b) => a.gap - b.gap);
  return waits.length ? { horizon: waits[0].h, inRange: false, gap: waits[0].gap } : { horizon: buys[0], inRange: false, gap: Infinity };
}

/** The horizon a card opens on; the list is sorted by exactly the number shown for it. */
export function displayedHorizon(row: Row): string | undefined {
  return entryFor(row).horizon;
}
export function displayedScore(row: Row): number {
  const h = displayedHorizon(row);
  const c = (h ? row.horizons[h]?.buy_confidence : null) ?? row.confidence;
  return c ? c.score : -1;
}
export function sortByDisplayedScore(rows: Row[]): Row[] {
  return [...rows].sort((a, b) => displayedScore(b) - displayedScore(a) || (b.confidence?.score ?? 0) - (a.confidence?.score ?? 0) || a.symbol.localeCompare(b.symbol));
}

function BuyCard({ row, onOpen, showAction = false, waiting = false }: { row: Row; onOpen: (row: Row) => void; showAction?: boolean; waiting?: boolean }) {
  const buyHorizons = HORIZONS.filter((h) => row.horizons[h]?.recommendation === 'BUY');
  const [chosen, setHorizon] = useState<string | null>(null);
  const horizon = chosen ?? displayedHorizon(row) ?? 'positional';
  const price = cardPrice(row);
  const active = HORIZONS.includes(horizon as never) ? horizon : buyHorizons[0];                // any horizon can be opened: a Hold or Avoid horizon explains itself instead of being a dead button
  const conf = row.horizons[active]?.buy_confidence ?? row.confidence;
  const levels = row.levels[active];
  const evid = evidenceChip(conf);
  return <article className={`rt-buy${showAction ? ` rt-buy-${row.bucket}` : ''}${waiting ? ' rt-buy-waiting' : ''}`}>
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
    <div className="rt-slot-status">{showAction && levels?.status !== 'available' && <p className="rt-setup rt-action" title={[row.action, ...(row.radar_reasons ?? [])].join(' · ')}>{row.action}{row.radar_reasons?.[0] ? ` · ${row.radar_reasons[0]}` : ''}</p>}{levels?.status === 'available' && <p className="rt-setup"><span className={`rt-chip ${cardState(levels, price).kind === 'in_band' ? 'rt-chip-ok' : 'rt-chip-warn'}`} title={cardState(levels, price).reason ?? cardState(levels, price).label}>{cardState(levels, price).compact}</span> {levels.selected_setup && <span className="rt-chip">{levels.selected_setup.replace(/_/g, ' ').toLowerCase()}</span>}{levels.breakout_trigger && <span className="rt-chip">trigger {inr(levels.breakout_trigger)}</span>}</p>}</div>
    <div className="rt-slot-ladder">{levels?.status === 'available' ? <Ladder levels={levels} lastClose={price} /> : row.horizons[active]?.recommendation !== 'BUY'
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
function LazyBuyCard({ row, onOpen, showAction = false, waiting = false }: { row: Row; onOpen: (row: Row) => void; showAction?: boolean; waiting?: boolean }) {
  const [full, setFull] = useState<Row | null>(null);
  const load = () => {
    if (full) return;
    void apiRequest<Row>(`/research/daily-report/${row.instrument_id}`).then((r) => { if (r && r.instrument_id === row.instrument_id) setFull(r); }).catch(() => undefined);
  };
  return <div onClickCapture={(e) => { if ((e.target as HTMLElement).closest('summary')) load(); }}><BuyCard row={full ? { ...row, ...full, price: full.price ?? row.price } : row} onOpen={onOpen} showAction={showAction} waiting={waiting} /></div>;
}

const STATUS_OF: Partial<Record<Bucket, { label: string; tone: 'positive' | 'caution' | 'info' | 'neutral'; order: number }>> = {
  radar: { label: 'Radar', tone: 'info', order: 0 }, hold: { label: 'Hold', tone: 'positive', order: 1 }, no_new_entry: { label: 'No new entry', tone: 'neutral', order: 2 },
  stale_data: { label: 'Not current', tone: 'caution', order: 3 }, not_enough_data: { label: 'Needs data', tone: 'caution', order: 4 },
};
const REC_TONE: Record<string, 'positive' | 'negative' | 'caution' | 'info' | 'neutral'> = { BUY: 'positive', HOLD: 'info', WAIT: 'caution', AVOID: 'negative' };

/** Everything that is not a buy tile, in one table styled like the multibagger screen: one status chip, one chip per horizon, one reason. */
const reasonOf = (row: Row): string => [
  row.researching ? 'Researching…' : row.action, ...(row.radar_reasons ?? []),
  ...HORIZONS.filter((h) => row.horizons[h]?.data_note).slice(0, 2).map((h) => `${HORIZON_NAME[h]}: ${row.horizons[h].data_note}`),
  row.data_note ?? (row.stale && !row.researching && row.researched_at ? 'A refresh is queued for tonight.' : null),
].filter(Boolean).join(' · ');

function OtherStocks({ rows, title, onOpen }: { rows: Row[]; title: string; onOpen: (row: Row) => void }) {
  const [query, setQuery] = useState('');
  const [heldOnly, setHeldOnly] = useState(false);
  const term = query.trim().toUpperCase();
  const shown = rows.filter((r) => (!term || r.symbol.toUpperCase().includes(term) || r.name.toUpperCase().includes(term)) && (!heldOnly || r.held))
    .sort((a, b) => (STATUS_OF[a.bucket]?.order ?? 9) - (STATUS_OF[b.bucket]?.order ?? 9) || a.symbol.localeCompare(b.symbol));
  const columns: Column<Row>[] = [
    { key: 'symbol', header: 'Stock', sticky: true, sortValue: (r) => r.symbol, render: (r) => <><strong>{r.symbol}</strong>{r.name !== r.symbol && <small className="ui-muted"> {r.name}</small>}</> },
    { key: 'status', header: 'Status', sortValue: (r) => STATUS_OF[r.bucket]?.order ?? 9, render: (r) => <Chip tone={STATUS_OF[r.bucket]?.tone ?? 'neutral'}>{STATUS_OF[r.bucket]?.label ?? r.bucket}</Chip> },
    { key: 'close', header: 'Last close', align: 'right', sortValue: (r) => (r.last_close == null ? null : Number(r.last_close)), render: (r) => inr(r.last_close) },
    ...HORIZONS.map((h): Column<Row> => ({ key: h, header: HORIZON_NAME[h], sortValue: (r) => r.horizons[h]?.score, firstSort: 'desc', render: (r) => { const rec = r.horizons[h]?.recommendation; const text = REC_TEXT[rec ?? ''] ?? '—'; return <Chip tone={REC_TONE[rec ?? ''] ?? 'neutral'} title={r.horizons[h]?.data_note ?? undefined}>{text}</Chip>; } })),
    { key: 'held', header: 'You hold', align: 'right', sortValue: (r) => (r.held ? Number(r.quantity ?? 0) : null), render: (r) => (r.held ? Number(r.quantity ?? 0).toLocaleString('en-IN') : '—') },
    { key: 'why', header: 'What to do', render: (r) => <span className="rt-why-cell" title={reasonOf(r)}>{reasonOf(r)}</span> },
    { key: 'when', header: 'Researched', sortValue: (r) => r.researched_at, render: (r) => <span className="ui-muted">{when(r.researched_at)}</span> },
  ];
  return <section className="rt-section" aria-label={title}>
    <div className="rt-section-head">
      <h3 className="rt-h">{title} <b className="rt-count">{shown.length}</b></h3>
      <div className="rt-v2-bar"><input type="search" placeholder="Search symbol or name" aria-label={`Search ${title}`} value={query} onChange={(e) => setQuery(e.target.value)} />
        <label><input type="checkbox" checked={heldOnly} onChange={(e) => setHeldOnly(e.target.checked)} /> Only stocks I hold</label></div>
    </div>
    <DataTable caption={title} columns={columns} rows={shown} rowKey={(r) => r.instrument_id} onRowClick={onOpen} virtualiseAbove={100} empty={rows.length ? 'No stock matches these filters.' : 'Nothing here right now.'} />
  </section>;
}

const PAGE = 24;

/** One group of research tiles with its own search and sort. Tiles are added 24 at a time so the page stays quick to scroll. */
function CardSection({ title, hint, empty, rows, onOpen, waiting = false }: { title: string; hint: string; empty: string; rows: Row[]; onOpen: (row: Row) => void; waiting?: boolean }) {
  const [query, setQuery] = useState('');
  const [sort, setSort] = useState<'near' | 'score' | 'symbol'>(waiting ? 'near' : 'score');
  const [limit, setLimit] = useState(PAGE);
  const term = query.trim().toUpperCase();
  const found = rows.filter((r) => !term || r.symbol.toUpperCase().includes(term) || r.name.toUpperCase().includes(term));
  const sorted = sort === 'symbol' ? [...found].sort((a, b) => a.symbol.localeCompare(b.symbol))
    : sort === 'near' ? sortByDisplayedScore(found).sort((a, b) => { const x = entryFor(a).gap; const y = entryFor(b).gap; return x === y ? 0 : x - y; })
      : sortByDisplayedScore(found);
  return <section className="rt-section" aria-label={title}>
    <div className="rt-section-head">
      <h3 className="rt-h">{title} <b className="rt-count">{found.length}</b> <small>{hint}</small></h3>
      {rows.length > 1 && <div className="rt-v2-bar"><input type="search" placeholder="Search symbol or name" aria-label={`Search ${title}`} value={query} onChange={(e) => setQuery(e.target.value)} />
        <select aria-label={`Sort ${title}`} value={sort} onChange={(e) => setSort(e.target.value as 'near' | 'score' | 'symbol')}>{waiting && <option value="near">Closest to buy range</option>}<option value="score">Highest score</option><option value="symbol">Symbol A to Z</option></select></div>}
    </div>
    {found.length === 0 ? <p className="rt-note">{rows.length === 0 ? empty : 'No match for this search.'}</p>
      : <div className="rt-buy-grid">{sorted.slice(0, limit).map((r) => <LazyBuyCard key={r.instrument_id} row={r} onOpen={onOpen} waiting={waiting} />)}</div>}
    {sorted.length > limit && <button type="button" className="rt-btn rt-ghost rt-more" onClick={() => setLimit(limit + PAGE)}>Show more ({sorted.length - limit} remaining)</button>}
  </section>;
}

/** Daily research report: swing, positional and long-term only. Refreshes itself; new stocks appear without a reload. */
export function DailyReport({ onOpen }: { onOpen: (instrument: Instrument) => void }) {
  const [report, setReport] = useState<Report | null>(null);
  const [error, setError] = useState('');
  const [tab, setTab] = useState<Bucket | 'all'>('all');
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
  const candidates = [...report.buckets.buy, ...(report.buckets.setup_waiting ?? [])];     // split here, on the price each tile shows now
  const inRange = candidates.filter((r) => entryFor(r).inRange);
  const waiting = candidates.filter((r) => !entryFor(r).inRange);
  const counts: Partial<Record<Bucket, number>> = { ...report.counts, buy: inRange.length, setup_waiting: waiting.length };
  const others = rows.filter((r) => r.bucket !== 'buy' && r.bucket !== 'setup_waiting');
  const otherTitle = tab === 'all' ? 'Everything else' : STATUS_OF[tab as Bucket]?.label ?? 'Everything else';
  const tiles: { key: Bucket; label: string; hint: string }[] = [
    { key: 'buy', label: 'Buy now', hint: 'price inside the buy range' }, { key: 'setup_waiting', label: 'Wait for entry', hint: 'price outside the range' }, { key: 'radar', label: 'Radar', hint: 'buy signal, no range yet' }, { key: 'hold', label: 'Hold', hint: 'in your portfolio' },
    { key: 'no_new_entry', label: 'No new entry', hint: 'not a buy now' }, { key: 'stale_data', label: 'Not current', hint: 'close or research is old' }, { key: 'not_enough_data', label: 'Needs data', hint: 'too little stored data' },
  ];
  return <section className="rt-daily" aria-label="Daily report">
    <div className="rt-daily-head">
      <div><h2>Today&apos;s research</h2><p className="rt-note">Swing, positional and long-term · {report.coverage?.universe_rule ?? `${total} stocks`} · updated {when(report.as_of)}</p></div>
      <span className={`rt-live${researching ? ' busy' : ''}`}>{researching ? `Researching ${report.researching?.join(', ')}…` : 'Auto-updating'}</span>
    </div>
    {error && <p className="rt-alert rt-alert-error" role="alert">{error}</p>}
    {report.coverage && <details className="rt-cover"><summary><b>Coverage</b> · {report.coverage.researched_current} of {report.coverage.universe} stocks researched and current{report.coverage.stale > 0 ? ` · ${report.coverage.stale} not current` : ''}{report.coverage.never_researched > 0 ? ` · ${report.coverage.never_researched} waiting` : ''} · last run {when(report.coverage.last_research_at)}</summary>
      <p className="rt-note"><b>Universe:</b> {report.coverage.universe_rule}</p><p className="rt-note"><b>Schedule:</b> {report.coverage.schedule}</p>
      <p className="rt-note">Every stock in the universe is listed below, including any that failed or are still waiting. Nothing is hidden.</p></details>}
    <div className="rt-tiles">{tiles.map((t) => <button key={t.key} type="button" className={`rt-tile rt-tile-${t.key}${tab === t.key ? ' active' : ''}`} onClick={() => setTab(tab === t.key ? 'all' : t.key)}><b>{counts[t.key] ?? 0}</b><span>{t.label}</span><small>{t.hint}</small></button>)}</div>
    <div className="rt-seg rt-seg-wide" role="tablist" aria-label="Filter">{TABS.map((t) => <button key={t.key} type="button" role="tab" aria-selected={tab === t.key} className={tab === t.key ? 'active' : ''} onClick={() => setTab(t.key)}>{t.label}</button>)}</div>
    {total === 0 && <div className="rt-card rt-empty-state"><h3>Nothing to report yet</h3><p>Add a stock to a watchlist or record a holding. It is researched automatically and appears here within a minute or two.</p></div>}
    {total > 0 && (tab === 'all' || tab === 'buy') && <CardSection title="Buy now" hint="price is inside the buy range" empty="No stock is inside its buy range right now." rows={inRange} onOpen={open} />}
    {total > 0 && (tab === 'all' || tab === 'setup_waiting') && <CardSection title="Wait for entry" hint="a buy signal, but the price is outside the range" empty="No stock is waiting for an entry." rows={waiting} onOpen={open} waiting />}
    {total > 0 && tab !== 'buy' && tab !== 'setup_waiting' && <OtherStocks key={tab} title={otherTitle} rows={others} onOpen={open} />}
    <p className="rt-disclaimer">{report.disclaimer}</p>
  </section>;
}
