import { inr } from './DailyReport';
import type { HorizonKey } from './researchTypes';

type Zone = { low: string; high: string; strength: number; labels: string[] };
type Comp = { score: number | null; why: string };
export type EngineHorizon = {
  status: string; reason?: string; primary_timeframe?: string; confirmation_timeframe?: string; last_completed_candle?: string;
  direction?: string; technical_score?: number; market_regime?: string; selected_setup?: string; entry_status?: string; current_price?: string;
  components?: Record<string, Comp>; scenarios?: Record<string, { status: string; reason: string; zone?: string[]; trigger?: string; extension_atr?: number; extension?: string }>;
  entry?: { low: string; high: string; planned_price: string; breakout_trigger: string | null; extension_atr?: number; extension?: string } | null;
  limit_entry?: { price: string; stop: string; fill_rate_in_replays: string; derivation: string; evidence: string } | null;
  stop_loss?: { price: string; percent: number; atr_units: number; method: string; reason: string; structural_invalidation: string; } | null;
  targets?: { label: string; price: string; risk_reward: number; basis: string }[]; enough_upside?: boolean | null;
  atr?: { atr14: number; natr: number }; indicators?: Record<string, number | null>;
  support_levels?: Zone[]; resistance_levels?: Zone[]; pivots?: Record<string, Record<string, string>>; relative_strength?: { available: boolean; note: string };
  reasons?: string[]; risks?: string[]; explanation?: string;
};
export type Engine = { status: string; reason?: string; last_daily_candle?: string; horizons: Record<string, EngineHorizon>; label?: string; validation?: string };

const NAME: Record<string, string> = { swing: 'Swing', positional: 'Positional', long_term: 'Long-term' };
const SETUP: Record<string, string> = { PULLBACK_ENTRY: 'Pullback entry', BREAKOUT_ENTRY: 'Breakout entry', MOMENTUM_ENTRY: 'Momentum entry', NO_VALID_SETUP: 'No valid setup' };
const STATUS: Record<string, string> = { ENTER_NOW: 'In buy band now', WAIT_FOR_ENTRY: 'Wait for entry', NO_VALID_SETUP: 'No valid setup', WAIT: 'Wait', INVALID: 'Not valid' };
const dir = (d?: string) => (d ? d.replace('_', ' ').toLowerCase() : '—');
const tone = (d?: string) => (d?.includes('BULL') ? 'good' : d?.includes('BEAR') ? 'bad' : 'mid');
const label = (k: string) => k.replace(/_/g, ' ');

export function EngineHeadline({ engine }: { engine: Engine | undefined }) {
  if (!engine || !engine.horizons) return null;
  return <div className="rt-eng-strip" aria-label="Technical direction by horizon">
    {(['swing', 'positional', 'long_term'] as HorizonKey[]).map((h) => { const v = engine.horizons[h]; return <span key={h} className={`rt-chip rt-chip-${v?.status === 'available' ? tone(v.direction) : 'mute'}`}>
      {NAME[h]}: {v?.status === 'available' ? `${dir(v.direction)} · ${Math.round(v.technical_score ?? 0)}` : 'not scored'}</span>; })}
  </div>;
}

/** Technical evidence for one horizon: direction, regime, entry scenarios, risk management, indicators, levels and a plain-language reading. */
export function TechnicalEnginePanel({ engine, horizon }: { engine: Engine | undefined; horizon: HorizonKey }) {
  if (!engine) return <p className="rt-empty">This report predates the technical engine. Re-run research.</p>;
  if (engine.status !== 'available' && engine.status !== undefined && !engine.horizons?.[horizon]) return <p className="rt-empty">{engine.reason ?? 'Technical analysis is not available.'}</p>;
  const h = engine.horizons?.[horizon];
  if (!h) return <p className="rt-empty">{engine.reason ?? 'Technical analysis is not available.'}</p>;
  if (h.status !== 'available') return <p className="rt-empty">{NAME[horizon]}: {h.reason}</p>;
  return <div className="rt-eng">
    <div className="rt-eng-top">
      <div><small>{NAME[horizon]} · {h.primary_timeframe} candles (confirmed on {h.confirmation_timeframe})</small>
        <h4 className={`rt-${tone(h.direction)}`}>{dir(h.direction)} · {Math.round(h.technical_score ?? 0)}/100</h4></div>
      <span className="rt-chip">{label(h.market_regime ?? '')}</span><span className="rt-chip">{SETUP[h.selected_setup ?? ''] ?? h.selected_setup}</span>
      <span className={`rt-chip ${h.entry_status === 'ENTER_NOW' ? 'rt-chip-ok' : 'rt-chip-warn'}`}>{STATUS[h.entry_status ?? ''] ?? h.entry_status}</span>
    </div>
    <p className="rt-eng-explain">{h.explanation}</p>
    <div className="rt-eng-grid">
      <div className="rt-card"><h4>Entry</h4>
        <dl className="rt-kv"><div><dt>Current price</dt><dd>{inr(h.current_price)}</dd></div>
          {h.entry ? <><div><dt>Ideal entry zone</dt><dd>{inr(h.entry.low)} – {inr(h.entry.high)}</dd></div><div><dt>Planned price</dt><dd>{inr(h.entry.planned_price)}</dd></div>
            {h.entry.breakout_trigger && <div><dt>Breakout trigger</dt><dd>{inr(h.entry.breakout_trigger)}</dd></div>}
            {h.entry.extension_atr !== undefined && <div><dt>Extension</dt><dd>{h.entry.extension_atr} ATR · {label(h.entry.extension ?? '')}</dd></div>}</> : <div><dt>Entry</dt><dd>None offered</dd></div>}</dl>
        <h4>Scenarios</h4><ul className="rt-list">{Object.entries(h.scenarios ?? {}).map(([k, s]) => <li key={k}><b>{SETUP[k] ?? k}</b> <span className="rt-chip">{STATUS[s.status] ?? s.status}</span><br /><small>{s.reason}</small></li>)}</ul></div>
      <div className="rt-card"><h4>Risk management</h4>
        {h.limit_entry && <><dl className="rt-kv"><div><dt>Pullback limit entry (best-tested)</dt><dd>{inr(h.limit_entry.price)} · stop {inr(h.limit_entry.stop)}</dd></div><div><dt>Fills</dt><dd>{h.limit_entry.fill_rate_in_replays}</dd></div></dl>
          <p className="rt-note">{h.limit_entry.derivation} {h.limit_entry.evidence} Decision support, not investment advice.</p></>}
        {h.stop_loss ? <dl className="rt-kv"><div><dt>Stop-loss</dt><dd>{inr(h.stop_loss.price)} ({h.stop_loss.percent}% · {h.stop_loss.atr_units} ATR)</dd></div><div><dt>Method</dt><dd>{label(h.stop_loss.method.toLowerCase())}</dd></div>
          <div><dt>Structural invalidation</dt><dd>{inr(h.stop_loss.structural_invalidation)}</dd></div></dl> : <p className="rt-empty">No stop because no valid setup.</p>}
        {h.stop_loss && <p className="rt-note">{h.stop_loss.reason}</p>}
        {h.targets && h.targets.length > 0 && <div className="rt-table-wrap"><table className="rt-table"><thead><tr><th>Target</th><th>Price</th><th>R:R</th><th>Basis</th></tr></thead>
          <tbody>{h.targets.map((t) => <tr key={t.label}><th scope="row">{t.label}</th><td>{inr(t.price)}</td><td>{t.risk_reward}</td><td>{t.basis}</td></tr>)}</tbody></table></div>}
        {h.enough_upside === false && <p className="rt-note">The first target offers less upside than this horizon requires, so entry waits.</p>}
        {h.atr && <p className="rt-note">ATR14 {h.atr.atr14} · NATR {h.atr.natr}%</p>}</div>
    </div>
    <h4>Score components</h4>
    <ul className="rt-parts">{Object.entries(h.components ?? {}).map(([k, c]) => <li key={k}><span>{label(k)}<small> · {c.why}</small></span><div className="rt-bar"><i style={{ width: `${c.score ?? 0}%` }} /></div><b>{c.score === null ? '—' : Math.round(c.score)}</b></li>)}</ul>
    <div className="rt-eng-grid">
      <div className="rt-card"><h4>Support zones</h4><ul className="rt-list">{(h.support_levels ?? []).map((z) => <li key={z.low}>{inr(z.low)} – {inr(z.high)} <small>strength {z.strength} · {z.labels.slice(0, 3).join(', ')}</small></li>)}</ul></div>
      <div className="rt-card"><h4>Resistance zones</h4><ul className="rt-list">{(h.resistance_levels ?? []).map((z) => <li key={z.low}>{inr(z.low)} – {inr(z.high)} <small>strength {z.strength} · {z.labels.slice(0, 3).join(', ')}</small></li>)}</ul></div>
    </div>
    {h.pivots && <details className="rt-fold"><summary>Classic pivot points (from the last completed day, week and month)</summary>
      <div className="rt-table-wrap"><table className="rt-table"><thead><tr><th></th>{['S2', 'S1', 'P', 'R1', 'R2'].map((c) => <th key={c}>{c}</th>)}</tr></thead>
        <tbody>{Object.entries(h.pivots).map(([name, d]) => <tr key={name}><th scope="row">{name}</th>{['S2', 'S1', 'P', 'R1', 'R2'].map((c) => <td key={c}>{inr(d[c])}</td>)}</tr>)}</tbody></table></div></details>}
    {h.relative_strength && <p className="rt-note"><b>Relative strength (context):</b> {h.relative_strength.note}</p>}
    <details className="rt-fold"><summary>Indicator values</summary><dl className="rt-kv">{Object.entries(h.indicators ?? {}).map(([k, v]) => <div key={k}><dt>{label(k)}</dt><dd>{v === null ? '—' : v}</dd></div>)}</dl></details>
    {(h.risks?.length ?? 0) > 0 && <div className="rt-reasons rt-reasons-warn"><strong>Risks and gaps</strong><ul>{h.risks!.slice(0, 6).map((r) => <li key={r}>{r}</li>)}</ul></div>}
    {engine.validation && <p className="rt-note"><b>Validation:</b> {engine.validation}</p>}
    <p className="rt-disclaimer">{engine.label} Last completed candle {h.last_completed_candle}.</p>
  </div>;
}
