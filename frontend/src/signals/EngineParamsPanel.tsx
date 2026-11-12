import { useMemo } from 'react';
import { geometry, bars, pct, type EngineValues, type GeometryInputs } from './engineGeometry';
import { Field } from './shared';

export type EngineParamSpec = { key: string; label: string; unit?: string; info?: string; text?: boolean };
export type EngineGroup = { title: string; params: EngineParamSpec[] };

/** Four groups in the order the task specifies. Long explanations live in the ⓘ hover, not inline. */
export const ENGINE_GROUPS: EngineGroup[] = [
  { title: 'Signals & timing', params: [
    { key: 'signal_types', label: 'Signal types', text: true, info: 'Comma-separated subset of T (trend pullback), R (band reversion), B (value-area breakout).' },
    { key: 'bar_minutes', label: 'Bar size', unit: 'min', info: 'Decision candles are built from stored 1-minute bars at this size.' },
    { key: 'H_minutes', label: 'Horizon H', unit: 'min', info: 'Maximum hold. Also sets sigma_H, the volatility the stop is derived from.' },
    { key: 'entry_mode', label: 'Entry mode', text: true, info: 'market: next open as taker. limit: at the signal close, needs strict penetration, 2-bar TTL.' },
    { key: 'no_entry_hours_ist', label: 'No-entry IST hours', text: true, info: 'Clock hours (IST) in which no new entry is taken, e.g. 11,12,13. Exits are unaffected.' },
  ] },
  { title: 'Barrier geometry', params: [
    { key: 'r', label: 'Reward : risk', unit: 'ratio', info: 'target = r × stop. Break-even win rate p* = (1 + cost/stop) / (r + 1).' },
    { key: 'k_vol', label: 'k_vol', unit: '×', info: 'Volatility stop = k_vol × sigma_H / r.' },
    { key: 'theta_feeR', label: 'θ (fee / R cap)', unit: 'ratio', info: 'Cost floor: the stop is never below cost / θ, so costs stay under θ of one R.' },
    { key: 's_cap_mult', label: 's-cap multiplier', unit: '×', info: 'A signal is skipped when the floored stop exceeds this multiple of the volatility stop: costs would dominate the trade.' },
  ] },
  { title: 'Exit rules', params: [
    { key: 'must_work_frac', label: 'Must-work share of target', unit: 'ratio', info: 'If the best favourable move is below this share of the target by the check time, exit at the next open.' },
    { key: 'must_work_at_frac', label: 'Must-work check at', unit: '× H', info: 'When the must-work check runs, as a fraction of H (spec: 1/3).' },
    { key: 'partial_pct', label: 'Partial exit', unit: '%', info: 'Share closed at +1R as maker. In India each partial is a separate order and pays brokerage.' },
  ] },
  { title: 'Risk', params: [
    { key: 'risk_pct', label: 'Risk per trade', unit: 'fraction', info: 'Notional = min(equity × risk / stop, equity × max leverage); quantity floors to the lot size.' },
  ] },
];

export type GeometryState = { status: 'idle' | 'loading' | 'ready' | 'unavailable'; inputs: GeometryInputs | null; symbol?: string; reason?: string };

const num = (raw: string | undefined, stored: unknown, fallback: number) => {
  const v = raw !== undefined && raw.trim() !== '' ? Number(raw) : Number(stored);
  return Number.isFinite(v) ? v : fallback;
};

export function EngineParamsPanel({ stored, values, onChange, geometry: geo }: {
  stored: Record<string, unknown>; values: Record<string, string>; onChange: (next: Record<string, string>) => void; geometry: GeometryState;
}) {
  const eff: EngineValues = useMemo(() => ({
    r: num(values.r, stored.r, 2), k_vol: num(values.k_vol, stored.k_vol, 1), theta_feeR: num(values.theta_feeR, stored.theta_feeR, 0.25), s_cap_mult: num(values.s_cap_mult, stored.s_cap_mult, 2),
    must_work_frac: num(values.must_work_frac, stored.must_work_frac, 0.3), must_work_at_frac: num(values.must_work_at_frac, stored.must_work_at_frac, 1 / 3),
    H_minutes: num(values.H_minutes, stored.H_minutes, 30), bar_minutes: num(values.bar_minutes, stored.bar_minutes, 5),
  }), [values, stored]);
  const g = geometry(eff, geo.inputs ?? { cost_rt_pct: null, sigma_H_median_pct: null, typical_bar_pct: null });
  const touched = Object.values(values).some((v) => v.trim() !== '');
  return (
    <details open className="sl-card sl-engine" aria-label="Engine parameters">
      <summary><strong>Engine parameters (scalp_hold)</strong> <span className="sl-muted">· blank = stored value{touched ? ' · overrides apply to this run only' : ''}</span></summary>
      <div className="sl-engine-body">
        <div className="sl-engine-form">
          {ENGINE_GROUPS.map((grp) => (
            <fieldset className="sl-engine-group" key={grp.title}>
              <legend>{grp.title}</legend>
              <div className="sl-engine-grid">
                {grp.params.map((p) => {
                  const placeholder = stored[p.key] === undefined ? 'stored' : stored[p.key] === '' ? (p.key === 'no_entry_hours_ist' ? 'none (e.g. 11,12,13)' : 'none') : String(stored[p.key]);
                  return (
                    <Field key={p.key} label={`${p.label}${p.unit ? ` (${p.unit})` : ''}`}>
                      <span className="sl-engine-input">
                        <input aria-label={`engine ${p.key}`} inputMode={p.text ? 'text' : 'decimal'} placeholder={placeholder} value={values[p.key] ?? ''} onChange={(e) => onChange({ ...values, [p.key]: e.target.value })} />
                        {p.info && <span className="sl-info" tabIndex={0} role="img" aria-label={`About ${p.label}: ${p.info}`} title={p.info}>i</span>}
                      </span>
                    </Field>
                  );
                })}
              </div>
            </fieldset>
          ))}
        </div>
        <aside className="sl-engine-preview" aria-label="Effective geometry">
          <h4>Effective geometry{geo.symbol ? ` · ${geo.symbol}` : ''}</h4>
          {geo.status === 'loading' && <p className="sl-muted">Measuring cost and typical bar range…</p>}
          {geo.status === 'unavailable' && <p className="sl-muted">Not measurable: {geo.reason ?? 'no stored data for the selected instrument and window'}. Formulas below use the entered values only.</p>}
          {geo.status === 'idle' && <p className="sl-muted">Pick an instrument to measure cost and typical bar range for this window.</p>}
          <dl className="sl-dl sl-engine-dl">
            <dt>round-trip cost</dt><dd>{pct(g.costRtPct)}</dd>
            <dt>cost floor (cost / θ)</dt><dd>{pct(g.floorPct)}</dd>
            <dt>volatility stop (k_vol · σ_H / r)</dt><dd>{pct(g.volStopPct)} <span className="sl-muted">median σ_H</span></dd>
            <dt>implied stop</dt><dd>{pct(g.stopPct)} <span className="sl-muted">{g.stopBars !== null ? bars(g.stopBars) : ''}</span></dd>
            <dt>target distance (r × stop)</dt><dd>{pct(g.targetPct)}</dd>
            <dt>break-even win rate p*</dt><dd>{g.pStar === null ? '—' : `${(g.pStar * 100).toFixed(1)}%`}</dd>
            <dt>must-work threshold</dt><dd>{pct(g.mustWorkPct)} <span className="sl-muted">{g.mustWorkInBars !== null ? `${bars(g.mustWorkInBars)} in ${g.mustWorkBars} bar${g.mustWorkBars === 1 ? '' : 's'}` : `checked after ${g.mustWorkBars} bar${g.mustWorkBars === 1 ? '' : 's'}`}</span></dd>
            <dt>s-cap: σ_H needed to trade</dt><dd>{pct(g.capThresholdSigmaPct)} {g.typicalCapped !== null && <span className={g.typicalCapped ? 'sl-warnText' : 'sl-pos'}>{g.typicalCapped ? '· a typical signal is skipped' : '· a typical signal trades'}</span>}</dd>
          </dl>
          <p className="sl-muted sl-engine-note">Stop uses the larger of the volatility stop and the cost floor; both are the engine's own formulas. Typical bar = median ATR(14) of this window.</p>
        </aside>
      </div>
    </details>
  );
}
