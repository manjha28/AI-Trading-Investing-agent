import { useMemo, useState } from 'react';
import { Empty, Notice, Pill, Scroll } from './shared';
import { show, type Cfg } from './varFormat';

type Row = { stage: string; reason: string; instrument: string | null; session: string; tod_bucket: string; setup: string | null; direction: string | null };

/** Candidate funnel: raw candidate -> executed trade, with every rejection attributed to a reason. Filterable by instrument, time of day and setup. */
export function VarFunnel({ result }: { result: Cfg }) {
  const f = result.funnel as Cfg | undefined;
  const rows = (f?.rows ?? []) as Row[];
  const [inst, setInst] = useState('');
  const [tod, setTod] = useState('');
  const [setup, setSetup] = useState('');
  const opts = useMemo(() => ({
    inst: [...new Set(rows.map((r) => r.instrument).filter(Boolean))].sort() as string[],
    tod: [...new Set(rows.map((r) => r.tod_bucket))].sort(),
    setup: [...new Set(rows.map((r) => r.setup).filter(Boolean))].sort() as string[],
  }), [rows]);
  const sel = useMemo(() => rows.filter((r) => (!inst || r.instrument === inst) && (!tod || r.tod_bucket === tod) && (!setup || r.setup === setup)), [rows, inst, tod, setup]);
  const counts = useMemo(() => {
    const by = (stage: string) => sel.filter((r) => r.stage === stage);
    const group = (xs: Row[]) => { const m = new Map<string, number>(); xs.forEach((r) => m.set(r.reason, (m.get(r.reason) ?? 0) + 1)); return [...m.entries()].sort((a, b) => b[1] - a[1]); };
    const strat = by('strategy'), risk = by('risk'), exe = by('execution'), sig = by('signal'), tr = by('trade');
    return { strat, risk, exe, sig: sig.length, tr: tr.length, cand: strat.length + sig.length, groups: { strategy: group(strat), risk: group(risk), execution: group(exe) } };
  }, [sel]);
  if (!f) return <Empty title="No funnel for this run">Run a backtest to see where candidates are rejected.</Empty>;
  const filtered = Boolean(inst || tod || setup);
  const pct = (n: number, d: number) => (d ? `${((n / d) * 100).toFixed(1)}%` : '—');
  return (
    <section className="sl-card" aria-label="Candidate funnel">
      <div className="sl-card-head"><h2>Candidate funnel</h2>
        <span>{f.reconciles ? <Pill tone="ok">reconciles</Pill> : <Pill tone="bad">does not reconcile</Pill>} <Pill tone="neutral">{String(f.engine)}</Pill></span></div>
      <p className="sl-muted">A candidate is a setup that reached its trigger bar. {String(f.reconciliation)}. A rejection is a result, not a bug: nothing here is loosened to admit more trades.</p>
      <div className="sl-grid2">
        <label>Instrument<select value={inst} onChange={(e) => setInst(e.target.value)}><option value="">All</option>{opts.inst.map((x) => <option key={x}>{x}</option>)}</select></label>
        <label>Time of day (IST, 30-min)<select value={tod} onChange={(e) => setTod(e.target.value)}><option value="">All</option>{opts.tod.map((x) => <option key={x}>{x}</option>)}</select></label>
        <label>Setup<select value={setup} onChange={(e) => setSetup(e.target.value)}><option value="">All</option>{opts.setup.map((x) => <option key={x}>{x}</option>)}</select></label>
      </div>
      {filtered && <Notice tone="info">Filtered view: counts below cover only the selected instrument / time bucket / setup. Reasons are the first condition that failed (exclusive).</Notice>}
      <Scroll label="Funnel">
        <table className="sl-table"><thead><tr><th>Step</th><th>Reason</th><th>n</th><th>% of candidates</th></tr></thead><tbody>
          <tr><th scope="row">Candidates generated</th><td /><td>{counts.cand}</td><td>100%</td></tr>
          {counts.groups.strategy.map(([r, n]) => <tr key={`s${r}`}><td>→ rejected (strategy)</td><td>{r}</td><td>{n}</td><td>{pct(n, counts.cand)}</td></tr>)}
          <tr><th scope="row">Signals generated</th><td /><td>{counts.sig}</td><td>{pct(counts.sig, counts.cand)}</td></tr>
          {counts.groups.risk.map(([r, n]) => <tr key={`r${r}`}><td>→ rejected (risk gate)</td><td>{r}</td><td>{n}</td><td>{pct(n, counts.cand)}</td></tr>)}
          {counts.groups.execution.map(([r, n]) => <tr key={`e${r}`}><td>→ not executed</td><td>{r}</td><td>{n}</td><td>{pct(n, counts.cand)}</td></tr>)}
          <tr><th scope="row">Trades executed</th><td /><td>{counts.tr}</td><td>{pct(counts.tr, counts.cand)}</td></tr>
        </tbody></table>
      </Scroll>
      {!filtered && (f.stages as Cfg[] | undefined)?.[0] && (
        <details><summary>Overlapping view: every failed condition per candidate</summary>
          <Scroll label="Any failed"><table className="sl-table"><thead><tr><th>Condition</th><th>Candidates failing it</th><th>% of strategy-rejected</th></tr></thead>
            <tbody>{(f.stages[0].any_failed as Cfg[]).map((x) => <tr key={x.reason}><td>{x.reason}</td><td>{x.n}</td><td>{show(x.pct_of_stage_rejected)}%</td></tr>)}</tbody></table></Scroll>
          <p className="sl-muted">A candidate that fails several conditions is counted once per condition, so these rows overlap and exceed the rejected total.</p></details>
      )}
      {f.pattern_stages && Object.keys(f.pattern_stages).length > 0 && (
        <details><summary>Pattern stages reached (unique setups before a candidate forms)</summary>
          <Scroll label="Pattern stages"><table className="sl-table"><tbody>{Object.entries(f.pattern_stages as Record<string, number>).map(([k, v]) => <tr key={k}><th scope="row">{k}</th><td>{v}</td></tr>)}</tbody></table></Scroll></details>
      )}
    </section>
  );
}
