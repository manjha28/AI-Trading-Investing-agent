import { useCallback, useEffect, useMemo, useState } from 'react';
import { apiRequest } from '../api';
import type { Instrument } from './researchTypes';

export type Levels = {
  status: string; reason?: string; entry_status?: string; selected_setup?: string; breakout_trigger?: string | null; stop_reason?: string; stop_method?: string; explanation?: string; direction?: string; technical_score?: number; market_regime?: string; risk_reward?: Record<string, number>;
  entry_zone?: { low: string; high: string }; stop?: string; reference_close?: string; prior_high_reference?: string;
  pivots?: { sets: Record<string, Record<string, string>>; nearest_support: { name: string; price: string } | null; nearest_resistance: { name: string; price: string } | null; rule: string } | null;
  targets?: { label: string; price: string; risk_reward?: number; basis?: string }[]; derivation?: { rule: string; inputs: Record<string, number>; tick: string };
};
export type Confidence = { score: number; band: 'High' | 'Medium' | 'Low'; note: string; parts: { key: string; label: string; value: number; weight: number }[]; horizon?: string };
type HorizonRec = { recommendation: string | null; score: number | null; confidence: number | null; buy_confidence?: Confidence | null; why_not: string[] };
type Bucket = 'buy' | 'hold' | 'no_new_entry' | 'not_enough_data';
type Row = {
  instrument_id: string; symbol: string; name: string; held: boolean; quantity: string | null; bucket: Bucket; action: string; researched_at: string | null; stale: boolean;
  horizons: Record<string, HorizonRec>; levels: Record<string, Levels | null>; last_close?: string | null; confidence?: Confidence | null; researching?: boolean; data_note?: string | null;
};
type Coverage = { universe: number; researched_current: number; stale: number; never_researched: number; last_research_at: string | null; universe_rule: string; schedule: string };
type Report = { coverage?: Coverage; as_of: string; buckets: Record<Bucket, Row[]>; counts: Record<Bucket, number>; scope: string; disclaimer: string; researching?: string[] };

const HORIZONS = ['swing', 'positional', 'long_term'] as const;
const HORIZON_NAME: Record<string, string> = { swing: 'Swing', positional: 'Positional', long_term: 'Long-term' };
const HORIZON_SPAN: Record<string, string> = { swing: 'days to weeks', positional: 'weeks to months', long_term: 'months to years' };
const TABS: { key: Bucket | 'all'; label: string }[] = [
  { key: 'all', label: 'All' }, { key: 'buy', label: 'Ready to buy' }, { key: 'hold', label: 'Hold' }, { key: 'no_new_entry', label: 'No new entry' }, { key: 'not_enough_data', label: 'Needs data' },
];
const REC_TEXT: Record<string, string> = { BUY: 'Buy', HOLD: 'Hold', AVOID: 'Avoid', WAIT: 'Wait', INSUFFICIENT_EVIDENCE: 'No data' };
const POLL_FAST_MS = 15_000;
const POLL_SLOW_MS = 300_000;

export const inr = (v?: string | number | null) => (v == null ? '—' : `₹${Number(v).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`);
export const when = (iso: string | null) => (iso ? new Date(iso).toLocaleString('en-IN', { timeZone: 'Asia/Kolkata', dateStyle: 'medium', timeStyle: 'short' }) : 'not yet');
const bandClass = (band?: string) => (band === 'High' ? 'good' : band === 'Medium' ? 'mid' : 'bad');

export function Ring({ score, band }: { score: number; band: string }) {
  const r = 26; const c = 2 * Math.PI * r;
  return <div className={`rt-ring rt-ring-${bandClass(band)}`} role="img" aria-label={`Confidence ${Math.round(score)} out of 100, ${band}`}>
    <svg viewBox="0 0 64 64" width="64" height="64" aria-hidden="true"><circle cx="32" cy="32" r={r} className="rt-ring-bg" /><circle cx="32" cy="32" r={r} className="rt-ring-fg" strokeDasharray={`${(score / 100) * c} ${c}`} transform="rotate(-90 32 32)" /></svg>
    <div><b>{Math.round(score)}</b><small>{band}</small></div>
  </div>;
}

/** Horizontal price ladder: stop, entry zone, last close and targets drawn to scale. */
export function Ladder({ levels }: { levels: Levels }) {
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
      <li className="mid"><small>{levels.entry_status === 'ENTER_NOW' ? 'Buy now between' : 'Wait to buy between'}</small><b>{inr(levels.entry_zone?.low)} – {inr(levels.entry_zone?.high)}</b></li>
      {levels.targets?.map((t) => <li key={t.label} className="good" title={t.basis}><small>Target {t.label}{t.risk_reward ? ` · ${t.risk_reward}R` : ''}</small><b>{inr(t.price)}</b></li>)}
    </ul>
  </div>;
}

function BuyCard({ row, onOpen }: { row: Row; onOpen: (row: Row) => void }) {
  const buyHorizons = HORIZONS.filter((h) => row.horizons[h]?.recommendation === 'BUY');
  const [horizon, setHorizon] = useState<string>(buyHorizons.includes('positional' as never) ? 'positional' : buyHorizons[0]);
  const active = buyHorizons.includes(horizon as never) ? horizon : buyHorizons[0];
  const conf = row.horizons[active]?.buy_confidence ?? row.confidence;
  const levels = row.levels[active];
  return <article className="rt-buy">
    <header>
      <div><button type="button" className="rt-sym" onClick={() => onOpen(row)}>{row.symbol}</button><span className="rt-name">{row.name}</span>
        <div className="rt-chips">{row.held && <span className="rt-chip rt-chip-held" title={`You hold ${row.quantity}`}>In portfolio</span>}{row.stale && <span className="rt-chip rt-chip-warn">Not current</span>}</div></div>
      {conf && <Ring score={conf.score} band={conf.band} />}
    </header>
    <div className="rt-seg" role="tablist" aria-label={`${row.symbol} horizon`}>
      {HORIZONS.map((h) => {
        const rec = row.horizons[h]?.recommendation;
        const on = rec === 'BUY';
        return <button key={h} type="button" role="tab" aria-selected={active === h} disabled={!on} className={active === h ? 'active' : ''} onClick={() => setHorizon(h)} title={on ? HORIZON_SPAN[h] : `${REC_TEXT[rec ?? ''] ?? '—'} on this horizon`}>
          {HORIZON_NAME[h]}<small>{on ? 'Buy' : REC_TEXT[rec ?? ''] ?? '—'}</small></button>;
      })}
    </div>
    {levels?.status === 'available' && <p className="rt-setup"><span className={`rt-chip ${levels.entry_status === 'ENTER_NOW' ? 'rt-chip-ok' : 'rt-chip-warn'}`}>{levels.entry_status === 'ENTER_NOW' ? 'Enter now' : 'Wait for entry'}</span> <span className="rt-chip">{(levels.selected_setup ?? '').replace(/_/g, ' ').toLowerCase()}</span>{levels.breakout_trigger && <span className="rt-chip">trigger {inr(levels.breakout_trigger)}</span>}</p>}
    {levels?.status === 'available' ? <Ladder levels={levels} /> : <p className="rt-note">{levels?.reason ?? 'No levels for this horizon.'}</p>}
    {levels?.status === 'available' && <p className="rt-note">Last close {inr(levels.reference_close)} · prior high {inr(levels.prior_high_reference)} · {HORIZON_SPAN[active]}</p>}
    {levels?.status === 'available' && levels.pivots && <p className="rt-note">Pivot support {levels.pivots.nearest_support ? `${inr(levels.pivots.nearest_support.price)} (${levels.pivots.nearest_support.name})` : '—'} · pivot resistance {levels.pivots.nearest_resistance ? `${inr(levels.pivots.nearest_resistance.price)} (${levels.pivots.nearest_resistance.name})` : '—'}</p>}
    {conf && <details className="rt-fold rt-why"><summary>Why {Math.round(conf.score)}/100?</summary>
      <ul className="rt-parts">{conf.parts.map((p) => <li key={p.key}><span>{p.label}<small> · {Math.round(p.weight * 100)}% weight</small></span><div className="rt-bar"><i style={{ width: `${p.value}%` }} /></div><b>{Math.round(p.value)}</b></li>)}</ul>
      <p className="rt-note">{conf.note} Provisional ranking of conviction, not a probability of profit.</p>
      {levels?.explanation && <p className="rt-note">{levels.explanation}</p>}{levels?.derivation && <p className="rt-note"><b>Levels:</b> {levels.derivation.rule}</p>}</details>}
    <footer><small>Researched {when(row.researched_at)}</small><button type="button" className="rt-btn rt-ghost" onClick={() => onOpen(row)}>Full research →</button></footer>
  </article>;
}

function CompactRow({ row, onOpen }: { row: Row; onOpen: (row: Row) => void }) {
  return <tr className={row.researching ? 'rt-skel' : ''}>
    <td><button type="button" className="rt-sym" onClick={() => onOpen(row)}>{row.symbol}</button><small className="rt-name">{row.name}</small></td>
    <td>{row.held ? <span className="rt-chip rt-chip-held">Holding {row.quantity ? Number(row.quantity).toLocaleString('en-IN') : ''}</span> : <span className="rt-muted">—</span>}</td>
    <td className="rt-act">{row.researching ? <span className="rt-spin">Researching…</span> : row.action}{row.data_note && <small className="rt-datanote">{row.data_note}</small>}{row.stale && !row.data_note && !row.researching && row.researched_at && <small className="rt-datanote">Last researched {when(row.researched_at)}; a refresh is queued for tonight's run.</small>}</td>
    <td className="rt-dots">{HORIZONS.map((h) => { const rec = row.horizons[h]?.recommendation; return <span key={h} className={`rt-dot rt-dot-${rec ?? 'none'}`} title={`${HORIZON_NAME[h]}: ${REC_TEXT[rec ?? ''] ?? '—'}`}>{HORIZON_NAME[h][0]}</span>; })}</td>
    <td className="r">{inr(row.last_close)}</td>
    <td className="rt-muted">{when(row.researched_at)}</td>
  </tr>;
}

/** Daily research report: swing, positional and long-term only. Refreshes itself; new stocks appear without a reload. */
export function DailyReport({ onOpen }: { onOpen: (instrument: Instrument) => void }) {
  const [report, setReport] = useState<Report | null>(null);
  const [error, setError] = useState('');
  const [tab, setTab] = useState<Bucket | 'all'>('all');
  const [limit, setLimit] = useState(25);
  const load = useCallback(() => apiRequest<Report>('/research/daily-report').then((r) => { setReport(r); setError(''); }).catch(() => setError('The daily report could not be loaded. Retrying automatically.')), []);
  const researching = (report?.researching?.length ?? 0) > 0;
  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), researching ? POLL_FAST_MS : POLL_SLOW_MS);
    const onFocus = () => void load();
    window.addEventListener('focus', onFocus);
    return () => { window.clearInterval(timer); window.removeEventListener('focus', onFocus); };
  }, [load, researching]);
  const open = (row: Row) => onOpen({ id: row.instrument_id, symbol: row.symbol, display_name: row.name, venue: 'NSE', is_synthetic: false });
  const rows = useMemo(() => (report ? (tab === 'all' ? (Object.values(report.buckets).flat() as Row[]) : report.buckets[tab]) : []), [report, tab]);
  if (!report) return <section className="rt-card rt-daily" aria-label="Daily report">{error ? <p className="rt-alert rt-alert-error" role="alert">{error}</p> : <div className="rt-skel-block" aria-busy="true">Loading the daily report…</div>}</section>;
  const total = Object.values(report.counts).reduce((a, b) => a + b, 0);
  const buys = tab === 'all' || tab === 'buy' ? report.buckets.buy : [];
  const allOthers = rows.filter((r) => r.bucket !== 'buy');
  const others = allOthers.slice(0, limit);
  const tiles: { key: Bucket; label: string; hint: string }[] = [
    { key: 'buy', label: 'Ready to buy', hint: 'with levels' }, { key: 'hold', label: 'Hold', hint: 'in your portfolio' },
    { key: 'no_new_entry', label: 'No new entry', hint: 'not a buy now' }, { key: 'not_enough_data', label: 'Needs data', hint: 'not current' },
  ];
  return <section className="rt-daily" aria-label="Daily report">
    <div className="rt-daily-head">
      <div><h2>Today&apos;s research</h2><p className="rt-note">Swing, positional and long-term · {total} stocks: your watchlist, portfolio and the Market watch master list · updated {when(report.as_of)}</p></div>
      <span className={`rt-live${researching ? ' busy' : ''}`}>{researching ? `Researching ${report.researching?.join(', ')}…` : 'Auto-updating'}</span>
    </div>
    {error && <p className="rt-alert rt-alert-error" role="alert">{error}</p>}
    {report.coverage && <details className="rt-cover"><summary><b>Coverage</b> · {report.coverage.researched_current} of {report.coverage.universe} stocks researched and current{report.coverage.stale > 0 ? ` · ${report.coverage.stale} not current` : ''}{report.coverage.never_researched > 0 ? ` · ${report.coverage.never_researched} waiting` : ''} · last run {when(report.coverage.last_research_at)}</summary>
      <p className="rt-note"><b>Universe:</b> {report.coverage.universe_rule}</p><p className="rt-note"><b>Schedule:</b> {report.coverage.schedule}</p>
      <p className="rt-note">Every stock in the universe is listed below, including any that failed or are still waiting. Nothing is hidden.</p></details>}
    <div className="rt-tiles">{tiles.map((t) => <button key={t.key} type="button" className={`rt-tile rt-tile-${t.key}${tab === t.key ? ' active' : ''}`} onClick={() => setTab(tab === t.key ? 'all' : t.key)}><b>{report.counts[t.key]}</b><span>{t.label}</span><small>{t.hint}</small></button>)}</div>
    <div className="rt-seg rt-seg-wide" role="tablist" aria-label="Filter">{TABS.map((t) => <button key={t.key} type="button" role="tab" aria-selected={tab === t.key} className={tab === t.key ? 'active' : ''} onClick={() => setTab(t.key)}>{t.label}</button>)}</div>
    {total === 0 && <div className="rt-card rt-empty-state"><h3>Nothing to report yet</h3><p>Add a stock to a watchlist or record a holding. It is researched automatically and appears here within a minute or two.</p></div>}
    {buys.length > 0 && <><h3 className="rt-h">Ready to buy <small>highest confidence first</small></h3><div className="rt-buy-grid">{buys.map((r) => <BuyCard key={r.instrument_id} row={r} onOpen={open} />)}</div></>}
    {others.length > 0 && <div className="rt-card rt-compact"><div className="rt-table-wrap"><table className="rt-table"><thead><tr><th>Stock</th><th>Portfolio</th><th>What to do</th><th title="Swing / Positional / Long-term">S · P · L</th><th className="r">Last close</th><th>Researched</th></tr></thead>
      <tbody>{others.map((r) => <CompactRow key={r.instrument_id} row={r} onOpen={open} />)}</tbody></table></div>
      {allOthers.length > limit && <p className="rt-note"><button type="button" className="rt-btn rt-ghost" onClick={() => setLimit(limit + 100)}>Show more ({allOthers.length - limit} remaining)</button></p>}
      <p className="rt-note"><span className="rt-dot rt-dot-BUY">B</span> buy <span className="rt-dot rt-dot-HOLD">H</span> hold <span className="rt-dot rt-dot-WAIT">W</span> wait <span className="rt-dot rt-dot-AVOID">A</span> avoid <span className="rt-dot rt-dot-none">–</span> no data</p></div>}
    <p className="rt-disclaimer">{report.disclaimer}</p>
  </section>;
}
