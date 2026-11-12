import { useEffect, useRef, useState } from 'react';
import { apiRequest } from '../api';
import { messageOf } from './lab';
import { Notice, Pill } from './shared';
import type { BackfillInstrument, BackfillJob, HealthRow } from './types';

const pct = (value: number | null | undefined) => (value == null ? '—' : `${value}%`);
const isoDay = (d: Date) => d.toISOString().slice(0, 10);

function Countdown({ until }: { until: string }) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => { const t = window.setInterval(() => setNow(Date.now()), 1000); return () => window.clearInterval(t); }, []);
  return <>{Math.max(0, Math.ceil((new Date(until).getTime() - now) / 1000))}s</>;
}

const STATUS_LABEL: Record<BackfillInstrument['status'], string> = {
  queued: 'Queued', running: 'Fetching…', waiting_rate_limit: 'Waiting (Dhan rate limit)', completed: 'Done', unrecoverable_gaps: 'Done · provider has no data for some minutes', failed: 'Failed',
};

export function BackfillPanel({ rows, onFinished }: { rows: HealthRow[]; onFinished: () => void }) {
  const today = new Date();
  const [start, setStart] = useState(isoDay(new Date(today.getTime() - 30 * 86_400_000)));
  const [end, setEnd] = useState(isoDay(today));
  const [chosen, setChosen] = useState<string[]>([]);
  const [job, setJob] = useState<BackfillJob | null>(null);
  const [error, setError] = useState<string | null>(null);
  const finishedFor = useRef<string | null>(null);

  useEffect(() => {
    if (!job || job.status === 'completed' || job.status === 'failed') {
      if (job && finishedFor.current !== job.id) { finishedFor.current = job.id; onFinished(); }
      return undefined;
    }
    const timer = window.setInterval(() => {
      apiRequest<BackfillJob>(`/market-data/history/sync-jobs/${job.id}`).then(setJob).catch((e: unknown) => setError(messageOf(e)));
    }, 2000);
    return () => window.clearInterval(timer);
  }, [job, onFinished]);

  const running = job !== null && (job.status === 'queued' || job.status === 'running');
  const toggle = (id: string) => setChosen((cur) => (cur.includes(id) ? cur.filter((x) => x !== id) : [...cur, id]));
  const start1m = async () => {
    setError(null);
    try {
      setJob(await apiRequest<BackfillJob>('/market-data/backfill/intraday-1m', { method: 'POST', body: JSON.stringify({ instrument_ids: chosen, start, end }) }));
    } catch (e) { setError(messageOf(e)); }
  };

  return (
    <section className="sl-card" aria-label="Backfill 1-minute data">
      <header className="sl-card-head"><h3>Backfill 1-minute data</h3></header>
      <p className="sl-muted">Fetches real candles from Dhan for the chosen instruments and window, then re-measures. Gaps the provider cannot supply stay gaps; nothing is filled or padded.</p>
      <div className="sl-row" style={{ display: 'flex', gap: 12, flexWrap: 'wrap', alignItems: 'center' }}>
        <label>From <input type="date" value={start} max={end} onChange={(e) => setStart(e.target.value)} disabled={running} /></label>
        <label>To <input type="date" value={end} min={start} onChange={(e) => setEnd(e.target.value)} disabled={running} /></label>
        <button type="button" disabled={running || chosen.length === 0} onClick={() => void start1m()}>{running ? 'Backfill running…' : `Backfill ${chosen.length || ''} selected`.trim()}</button>
      </div>
      <div className="sl-row" style={{ display: 'flex', gap: 10, flexWrap: 'wrap', margin: '8px 0' }} role="group" aria-label="Instruments to backfill">
        {rows.map((r) => <label key={r.instrument_id}><input type="checkbox" checked={chosen.includes(r.instrument_id)} onChange={() => toggle(r.instrument_id)} disabled={running} /> {r.symbol}</label>)}
      </div>
      {error && <Notice tone="bad">{error}</Notice>}
      {job && (
        <div aria-live="polite">
          <div className="sl-progress" role="progressbar" aria-valuemin={0} aria-valuemax={job.instruments_total} aria-valuenow={job.instruments_done}>
            <div style={{ width: `${(100 * job.instruments_done) / Math.max(1, job.instruments_total)}%` }} />
            <span>{job.instruments_done}/{job.instruments_total} instruments{job.current ? ` · now ${job.current.symbol}` : ''}</span>
          </div>
          {job.pause && <Notice tone="warn">Dhan is rate-limiting history requests. Waiting <strong><Countdown until={job.pause.until} /></strong> before retrying {job.pause.symbol} (retry {job.pause.attempt} of {job.pause.of}). This is expected, not a freeze.</Notice>}
          {job.status === 'failed' && <Notice tone="bad">Backfill stopped: {job.error ?? 'unknown error'}</Notice>}
          <table className="sl-table sl-compact"><thead><tr><th>Instrument</th><th>Status</th><th>Missing before</th><th>Missing after</th><th>Fetched</th><th>Detail</th></tr></thead><tbody>
            {job.instruments.map((i) => (
              <tr key={i.instrument_id}>
                <td>{i.symbol}</td><td>{STATUS_LABEL[i.status]}</td>
                <td className="sl-r">{pct(i.before?.missing_pct)}</td><td className="sl-r">{pct(i.after?.missing_pct)}</td><td className="sl-r">{i.bars_fetched.toLocaleString()} bars</td>
                <td>
                  {i.reason && <Pill tone="bad">{i.reason}</Pill>}
                  {i.provider_coverage && <div className="sl-muted">{i.provider_coverage.label}; excluded from the {job.refuse_threshold_pct}% refusal share</div>}
                  {i.unrecoverable.map((u, n) => <div key={n} className="sl-muted">{u.day ?? u.from ?? 'window'}{u.missing_minutes != null ? ` · ${u.missing_minutes} min` : ''}: {u.reason}</div>)}
                </td>
              </tr>
            ))}
          </tbody></table>
        </div>
      )}
    </section>
  );
}
