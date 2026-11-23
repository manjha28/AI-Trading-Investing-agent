import { useEffect, useState } from 'react';
import { apiRequest } from '../api';
import { formatIST } from '../lib/formatIST';

type Stats = { trades: number; t1_hits: number; hit_rate: number | null; hit_ci95: (number | null)[]; net_r: number | null; net_r_ci95: (number | null)[]; stopped: number; timed_out: number };
type Block = { all: Stats; by_verdict: Record<string, Stats>; blue_sky: Stats; not_blue_sky: Stats; reliability?: { bucket: number; n: number; predicted: number; actual: number }[] };
type LongBlock = Record<string, { n: number; median_excess: number; mean_excess: number | null; mean_ci95: (number | null)[]; beat_index: number } | string>;
type Report = {
  available: boolean; message?: string; as_of?: string; computed_at?: string; disclaimer: string;
  short_horizon?: { observations: number; first_signal: string | null; not_a_trade: Record<string, number>; by_horizon: Record<string, Record<string, Block>> };
  long_term?: { available: boolean; reason?: string; first_class_date?: string; benchmark?: string; by_months: Record<string, LongBlock> };
};

const pct = (x: number | null | undefined) => (x === null || x === undefined ? '—' : `${Math.round(x * 100)}%`);
const r2 = (x: number | null | undefined) => (x === null || x === undefined ? '—' : `${x > 0 ? '+' : ''}${x.toFixed(2)}R`);
const ci = (a: (number | null)[], f: (x: number | null) => string) => (a[0] === null || a[1] === null ? '' : ` [${f(a[0])}, ${f(a[1])}]`);
const NAMES: Record<string, string> = { swing: 'Swing (15 sessions)', positional: 'Positional (60 sessions)' };

function Row({ label, s }: { label: string; s: Stats }) {
  return <tr><td>{label}</td><td>{s.trades}</td><td>{pct(s.hit_rate)}{ci(s.hit_ci95, pct)}</td><td>{r2(s.net_r)}{ci(s.net_r_ci95, r2)}</td><td>{s.stopped} / {s.timed_out}</td></tr>;
}

/** Hidden page (open /#forward-record; not in the navigation): the weekly forward record of the point-in-time shadow verdicts. Hypothetical results, never portfolio P&L. */
export function ForwardRecordPage() {
  const [rep, setRep] = useState<Report | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let active = true;
    void apiRequest<Report>('/research/forward-record').then((d) => { if (active) setRep(d); }).catch((e: Error) => { if (active) setError(e.message || 'The forward record could not be loaded.'); });
    return () => { active = false; };
  }, []);
  if (error) return <p role="alert">{error}</p>;
  if (!rep) return <p className="rt-muted">Loading the forward record…</p>;
  if (!rep.available || !rep.short_horizon) return <section className="panel"><h2>Forward record</h2><p className="rt-muted">{rep.message}</p><p className="rt-muted">{rep.disclaimer}</p></section>;
  const sh = rep.short_horizon;
  return (
    <section className="panel" aria-label="Forward record">
      <h2>Forward record</h2>
      <p className="rt-muted">Weekly. Every shadow buy verdict old enough to have resolved, played forward on the bars that came after (the same card definition and delivery costs as the replays). Report of {rep.as_of}{rep.computed_at ? `, computed ${formatIST(rep.computed_at)}` : ''}. First stored signal: {sh.first_signal ?? '—'}. {rep.disclaimer}</p>
      {Object.keys(NAMES).map((h) => {
        const ep = sh.by_horizon[h]?.episodes; const all = sh.by_horizon[h]?.all_nights;
        if (!ep) return null;
        return (
          <div key={h}>
            <h3>{NAMES[h]}</h3>
            <table>
              <thead><tr><th>Group</th><th>Trades</th><th>T1 before stop [95% CI]</th><th>Net R [95% CI]</th><th>Stopped / timed out</th></tr></thead>
              <tbody>
                <Row label="All buy verdicts (first night of each run)" s={ep.all} />
                <Row label="Buy now" s={ep.by_verdict.buy_now} />
                <Row label="Buy on trigger" s={ep.by_verdict.buy_on_trigger} />
                <Row label="Blue-sky ladders" s={ep.blue_sky} />
                <Row label="Not blue sky" s={ep.not_blue_sky} />
                {all ? <Row label="Every night (overlapping, optimistic CI)" s={all.all} /> : null}
              </tbody>
            </table>
            {ep.reliability && ep.reliability.length > 0 ? (
              <table aria-label={`${h} reliability`}>
                <thead><tr><th>Predicted confidence bucket</th><th>Setups</th><th>Predicted</th><th>Actual win rate</th></tr></thead>
                <tbody>{ep.reliability.map((b) => <tr key={b.bucket}><td>{b.bucket}</td><td>{b.n}</td><td>{pct(b.predicted)}</td><td>{pct(b.actual)}</td></tr>)}</tbody>
              </table>
            ) : <p className="rt-muted">Reliability table appears once at least 10 resolved setups carry a stored confidence.</p>}
          </div>
        );
      })}
      <p className="rt-muted">Not trades (counted, never 0R): {Object.entries(sh.not_a_trade).map(([k, v]) => `${k.replace('_', ' ')} ${v}`).join(', ') || 'none'}.</p>
      <h3>Long-term classes vs the NIFTY 500</h3>
      {rep.long_term?.available ? (
        <>
          <p className="rt-muted">{rep.long_term.benchmark}. First stored class date {rep.long_term.first_class_date}. One reading per stock per week.</p>
          {Object.entries(rep.long_term.by_months).map(([m, block]) => (
            <div key={m}>
              <h4>{m} months</h4>
              {typeof (block as { note?: string }).note === 'string' ? <p className="rt-muted">{(block as { note?: string }).note}</p> : (
                <table>
                  <thead><tr><th>Class</th><th>Stocks</th><th>Median excess</th><th>Mean excess [95% CI]</th><th>Beat the index</th></tr></thead>
                  <tbody>{Object.entries(block).map(([k, v]) => typeof v === 'string' ? null : <tr key={k}><td>{k.replace('_', ' ')}</td><td>{v.n}</td><td>{pct(v.median_excess)}</td><td>{pct(v.mean_excess)}{ci(v.mean_ci95, pct)}</td><td>{pct(v.beat_index)}</td></tr>)}</tbody>
                </table>
              )}
            </div>
          ))}
        </>
      ) : <p className="rt-muted">{rep.long_term?.reason ?? 'No long-term record yet.'}</p>}
    </section>
  );
}
