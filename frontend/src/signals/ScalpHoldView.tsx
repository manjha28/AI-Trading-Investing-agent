import { Empty, Notice, Pill, Scroll, Stat } from './shared';

type Num = number | null | undefined;
type Report = { n?: number; mean_R?: Num; se_R?: Num; exit_reasons?: Record<string, number>; avgR_by_tod_bucket?: Record<string, number>; avgR_by_signal_type?: Record<string, number>; fee_R_mean?: Num };
type Instrument = {
  symbol: string; venue: string; bars_used: number; sessions_excluded: number; sessions_total: number; start: string; end: string; traded_from?: string; seasonal_fitted_through?: string | null;
  n_signals?: number; skips?: Record<string, number>; below_min_lot_rate?: Num; cost_report?: { cost_rt_pct?: Num; f_median?: Num; p_star?: Num }; report?: Report;
  folds?: Record<string, unknown>[]; warnings?: string[]; total_combos_evaluated?: number; matched_random?: { strategy_mean_R: Num; percentile: Num; control_means: number[] };
  sign_flip?: { n: number; mean_R: Num; se_R: Num }; phase0?: Record<string, unknown>[]; phase1?: Record<string, unknown>[];
};
export type ScalpHoldResult = {
  kind: string; slip_bps: number; unverified_assumptions: string[]; refused_instruments: { symbol: string; reason: string }[]; instruments: Instrument[]; trades_total: number;
  trades: Record<string, unknown>[]; summary: Report; note: string; total_combos_evaluated: number;
};

const f = (v: Num, d = 3) => (v == null ? '—' : v.toFixed(d));
const pct = (v: Num, d = 2) => (v == null ? '—' : `${(v * 100).toFixed(d)}%`);

function Table({ rows, cols, label }: { rows: Record<string, unknown>[]; cols: string[]; label: string }) {
  if (!rows.length) return <p className="sl-muted">No rows.</p>;
  return (
    <Scroll label={label}>
      <table className="sl-table sl-compact">
        <thead><tr>{cols.map((c) => <th key={c}>{c.replace(/_/g, ' ')}</th>)}</tr></thead>
        <tbody>{rows.map((r, i) => <tr key={i}>{cols.map((c) => <td key={c}>{typeof r[c] === 'number' ? (Number.isInteger(r[c]) ? String(r[c]) : (r[c] as number).toFixed(4)) : r[c] == null ? '—' : typeof r[c] === 'object' ? JSON.stringify(r[c]) : String(r[c])}</td>)}</tr>)}</tbody>
      </table>
    </Scroll>
  );
}

/** Results of a scalp_hold engine run: primary metric is mean R per trade with its standard error. Equity and drawdown are not ranked on. */
export function ScalpHoldView({ r }: { r: ScalpHoldResult }) {
  const s = r.summary;
  return (
    <div className="sl-scalp" aria-label="scalp_hold results">
      <Notice tone="warn">{r.note}</Notice>
      {r.unverified_assumptions.length > 0 && <Notice tone="warn"><ul>{r.unverified_assumptions.map((m) => <li key={m}>{m.replace(/^WARNING: /, '')}</li>)}</ul></Notice>}
      <div className="sl-stats" aria-label="Key results">
        <Stat label="Trades" value={r.trades_total} hint="Out-of-sample trades only: the first 40% (single run) or each fold's training span is never traded." />
        <Stat label="Mean R per trade" value={f(s.mean_R)} tone={s.mean_R == null ? undefined : s.mean_R >= 0 ? 'pos' : 'neg'} hint="Net of venue costs, in multiples of the risk taken. Primary metric." />
        <Stat label="Standard error" value={f(s.se_R)} />
        <Stat label="Fees (mean R)" value={f(s.fee_R_mean)} />
        {r.kind === 'walk_forward' && <Stat label="Combinations evaluated" value={r.total_combos_evaluated} hint="Every grid combination tried across folds. Cap: 500." />}
      </div>
      {r.refused_instruments.length > 0 && <Notice tone="bad"><strong>Not evaluated:</strong><ul>{r.refused_instruments.map((x) => <li key={x.symbol}>{x.symbol}: {x.reason}</li>)}</ul></Notice>}
      {r.instruments.length === 0 && <Empty title="Nothing evaluated">No instrument had enough clean data.</Empty>}
      {r.instruments.map((i) => (
        <section className="sl-card" key={i.symbol} aria-label={`${i.symbol} scalp_hold`}>
          <h3>{i.symbol} <Pill tone="info">{i.venue}</Pill></h3>
          <p className="sl-muted">
            {i.bars_used.toLocaleString()} five-minute bars ({i.start.slice(0, 10)} → {i.end.slice(0, 10)}); {i.sessions_excluded} of {i.sessions_total} sessions excluded for missing candles (never filled).
            {i.traded_from && ` Traded from ${i.traded_from.slice(0, 10)}; seasonality fitted through ${i.seasonal_fitted_through?.slice(0, 10) ?? '—'} only.`}
          </p>
          <div className="sl-stats">
            <Stat label="Mean R" value={f(i.report?.mean_R)} /><Stat label="Trades" value={i.report?.n ?? 0} />
            {i.cost_report && <Stat label="Round-trip cost" value={`${f(i.cost_report.cost_rt_pct, 4)}%`} hint="Entry and exit as taker, at the reference notional." />}
            {i.cost_report && <Stat label="p* (break-even win rate)" value={pct(i.cost_report.p_star, 1)} hint="(1 + f) / (r + 1), f = cost / stop distance." />}
            <Stat label="Below min lot" value={i.below_min_lot_rate == null ? '—' : pct(i.below_min_lot_rate, 1)} hint="Share of signals dropped because the account is too small for one lot." />
          </div>
          {i.report?.exit_reasons && <p>Exit reasons: {Object.entries(i.report.exit_reasons).map(([k, v]) => `${k.replace(/_/g, ' ')} ${(v * 100).toFixed(1)}%`).join(' · ')}</p>}
          {i.report?.avgR_by_signal_type && <p>Mean R by signal: {Object.entries(i.report.avgR_by_signal_type).map(([k, v]) => `${k} ${v.toFixed(3)}`).join(' · ')}</p>}
          {i.skips && Object.keys(i.skips).length > 0 && <p className="sl-muted">Skipped signals: {Object.entries(i.skips).map(([k, v]) => `${k.replace(/_/g, ' ')} ${v}`).join(' · ')}</p>}
          {(i.warnings ?? []).map((w) => <Notice key={w} tone="warn">{w}</Notice>)}
          {i.folds && <details open className="sl-issue-group"><summary><strong>Walk-forward folds</strong> (primary metric: mean R ± SE)</summary><Table label="Folds" rows={i.folds} cols={['fold', 'combo', 'waiver', 'partial_pct', 'slip_mult', 'n', 'mean_R', 'se_R', 'below_min_lot_rate']} /></details>}
          {i.matched_random && (
            <details open className="sl-issue-group"><summary><strong>Null controls</strong></summary>
              <p>Matched random entry (same stop/target/horizon, random bar and direction, {i.matched_random.control_means.length} seeds): strategy mean R {f(i.matched_random.strategy_mean_R)} sits at percentile <strong>{f(i.matched_random.percentile, 1)}</strong> of the controls.</p>
              {i.sign_flip && <p>Sign-flip (every signal inverted): mean R {f(i.sign_flip.mean_R)} over {i.sign_flip.n} trades.</p>}
              <p className="sl-muted">Block-bootstrap surrogates are run from the command line (<code>python -m scalp_hold phase3</code>) and are always tagged is_surrogate.</p>
            </details>
          )}
          <details className="sl-issue-group"><summary><strong>Phase 0</strong> base rate (unconditional triple-barrier)</summary><Table label="Phase 0" rows={i.phase0 ?? []} cols={['k_vol', 'r', 'H', 'n', 'p_baseline', 'theory_1_over_r1', 'timeout_rate', 'f', 'p_star', 'p_star_over_base']} /></details>
          <details className="sl-issue-group"><summary><strong>Phase 1</strong> signal edge over baseline (non-overlapping samples)</summary><Table label="Phase 1" rows={i.phase1 ?? []} cols={['signal', 'k_vol', 'r', 'H', 'n', 'p_win', 'p_baseline', 'edge', 't_stat']} /></details>
        </section>
      ))}
      <details className="sl-issue-group"><summary><strong>Trades</strong> (first {Math.min(100, r.trades.length)} of {r.trades_total})</summary>
        <Table label="Trades" rows={r.trades.slice(0, 100)} cols={['instrument', 'ts_entry', 'ts_exit', 'side', 'signal_type', 'entry_px', 'exit_px', 's', 'qty', 'net_pnl', 'R', 'fee_R', 'MFE_pct', 'MAE_pct', 'exit_reason', 'hold_minutes']} />
      </details>
    </div>
  );
}
