import { useCallback, useEffect, useState } from 'react';
import { lab, messageOf } from './lab';
import { fmtIst, suppressionText, withTimeout } from './lifecycle';
import { ConfirmDialog, Empty, Notice, Pill, Scroll } from './shared';
import type { JobRow, ScanRunView, ScheduleView, StrategyRow } from './types';

const JOB_GROUPS: { status: JobRow['status']; label: string; tone: 'ok' | 'warn' | 'bad' | 'info' | 'neutral' }[] = [
  { status: 'failed', label: 'Failed', tone: 'bad' }, { status: 'running', label: 'Running', tone: 'info' }, { status: 'queued', label: 'Queued', tone: 'warn' },
  { status: 'succeeded', label: 'Succeeded', tone: 'ok' }, { status: 'cancelled', label: 'Cancelled', tone: 'neutral' },
];
const TYPE_LABEL: Record<string, string> = { backtest: 'Backtest', scan: 'Scan' };

function useLoad<T>(path: string, pollWhile?: (value: T) => boolean) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(async () => {
    try { setData(await withTimeout(lab<T>(path))); setError(null); } catch (e) { setError(messageOf(e)); }
  }, [path]);
  useEffect(() => { void load(); }, [load]);
  const polling = data !== null && !!pollWhile?.(data);
  useEffect(() => {
    if (!polling) return undefined;
    const t = window.setInterval(() => { void load(); }, 3000);      // refreshes this view only while something is queued or running; it schedules nothing
    return () => window.clearInterval(t);
  }, [polling, load]);
  return { data, error, load };
}

export function JobsPanel() {
  const { data, error, load } = useLoad<JobRow[]>('/jobs?limit=100', (jobs) => jobs.some((j) => j.status === 'queued' || j.status === 'running'));
  const [actionError, setActionError] = useState<string | null>(null);
  const [cancelling, setCancelling] = useState<JobRow | null>(null);
  const [deleting, setDeleting] = useState<{ body: { job_type?: string; error?: string }; text: string } | null>(null);
  const remove = async () => {
    if (!deleting) return;
    setActionError(null);
    try { await withTimeout(lab<{ deleted: number }>('/jobs/delete-failed', { method: 'POST', body: deleting.body })); await load(); } catch (e) { setActionError(messageOf(e)); }
    setDeleting(null);
  };
  const act = async (job: JobRow, what: 'retry' | 'cancel') => {
    setActionError(null);
    try { await withTimeout(lab<JobRow>(`/jobs/${job.id}/${what}`, { method: 'POST', body: {} })); await load(); } catch (e) { setActionError(messageOf(e)); }
    setCancelling(null);
  };
  const describe = (j: JobRow) => (j.job_type === 'scan' ? `${TYPE_LABEL.scan} · ${j.payload.trigger === 'schedule' ? 'scheduled' : 'manual'}` : TYPE_LABEL[j.job_type] ?? j.job_type);
  return (
    <section className="sl-card" aria-label="Jobs">
      <header className="sl-card-head"><h3>Jobs</h3><button type="button" onClick={() => void load()}>Refresh</button></header>
      <p className="sl-muted">Backtests and scans run on a durable server queue: they survive restarts, retry with backoff, and report failures here. Times are IST.</p>
      {error && <Notice tone="bad">Could not load jobs: {error} <button type="button" className="sl-link" onClick={() => void load()}>Retry</button></Notice>}
      {actionError && <Notice tone="bad">{actionError}</Notice>}
      {!error && data === null && <p className="sl-muted" role="status">Loading jobs… (gives up after 15 s)</p>}
      {data !== null && data.length === 0 && <Empty title="No jobs yet">Running a backtest or a scan creates one.</Empty>}
      {data !== null && data.length > 0 && JOB_GROUPS.map((g) => {
        const rows = data.filter((j) => j.status === g.status);
        if (!rows.length) return null;
        const table = (list: JobRow[]) => (
            <Scroll label={`${g.label} jobs`}>
              <table className="sl-table sl-compact">
                <thead><tr><th>Job</th><th>Progress</th><th>Attempts</th><th>Created</th><th>Finished</th><th>Detail</th><th /></tr></thead>
                <tbody>
                  {list.map((j) => (
                    <tr key={j.id}>
                      <td>{describe(j)}<div className="sl-muted"><code>{j.id.slice(0, 8)}</code></div></td>
                      <td>
                        <div className="sl-progress" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Number(j.progress?.pct ?? 0)}><div style={{ width: `${Number(j.progress?.pct ?? 0)}%` }} /><span>{String(j.progress?.stage ?? j.status).replace(/_/g, ' ')}</span></div>
                      </td>
                      <td className="sl-r">{j.attempts}/{j.max_attempts}</td>
                      <td>{fmtIst(j.created_at)}</td><td>{fmtIst(j.finished_at)}</td>
                      <td>
                        {j.error && <Pill tone={j.status === 'failed' ? 'bad' : 'warn'}>{j.status === 'failed' ? 'error' : 'last error'}</Pill>}{' '}{j.error ?? ''}
                        {j.status === 'queued' && j.attempts > 0 && <div className="sl-muted">Retry scheduled for {fmtIst(j.next_attempt_at)}</div>}
                      </td>
                      <td>
                        {(j.status === 'failed' || j.status === 'cancelled') && <button type="button" onClick={() => void act(j, 'retry')}>Retry</button>}
                        {j.status === 'queued' && <button type="button" onClick={() => setCancelling(j)}>Cancel</button>}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </Scroll>
        );
        const finishedBadly = g.status === 'failed' || g.status === 'cancelled';
        // the same failure repeated is one line with a count, not one line per attempt
        const kinds = finishedBadly ? [...rows.reduce((m, j) => m.set(`${j.job_type}|${j.error ?? ''}`, [...(m.get(`${j.job_type}|${j.error ?? ''}`) ?? []), j]), new Map<string, JobRow[]>()).values()] : [];
        return (
          <details key={g.status} open={g.status === 'failed' || g.status === 'running' || g.status === 'queued'} className="sl-issue-group">
            <summary><Pill tone={g.tone}>{g.label}</Pill> {rows.length} job{rows.length === 1 ? '' : 's'}</summary>
            {g.status === 'failed' && <div className="sl-toolbar"><button type="button" onClick={() => setDeleting({ body: {}, text: 'every failed and cancelled job' })}>Delete all failed jobs</button></div>}
            {!finishedBadly && table(rows)}
            {kinds.map((list) => (
              <div key={`${list[0].job_type}|${list[0].error}`} className="sl-job-kind">
                <p><strong>{list.length} × {describe(list[0])}</strong> <span className="sl-muted">last on {fmtIst(list[0].created_at)}</span><br />{list[0].error ?? 'No error message was recorded.'}</p>
                <div className="sl-toolbar">
                  <button type="button" onClick={() => void act(list[0], 'retry')}>Run latest again</button>
                  <button type="button" onClick={() => setDeleting({ body: { job_type: list[0].job_type, ...(list[0].error ? { error: list[0].error } : {}) }, text: `all ${describe(list[0])} jobs with this error` })}>Delete these</button>
                </div>
                <details><summary>Show each run</summary>{table(list)}</details>
              </div>
            ))}
          </details>
        );
      })}
      {deleting && <ConfirmDialog title="Delete these jobs?" confirmLabel="Delete" danger onCancel={() => setDeleting(null)} onConfirm={() => void remove()} body={<p>This removes {deleting.text}, with their empty scan-history lines. It cannot be undone.</p>} />}
      {cancelling && <ConfirmDialog title="Cancel this queued job?" confirmLabel="Cancel job" danger onCancel={() => setCancelling(null)} onConfirm={() => void act(cancelling, 'cancel')} body={<p>It has not started. You can retry it later.</p>} />}
    </section>
  );
}

export function ScannerSchedulePanel({ strategies }: { strategies: StrategyRow[] }) {
  const { data, error, load } = useLoad<ScheduleView>('/scanner/schedule');
  const [view, setView] = useState<ScheduleView | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  useEffect(() => { setView(data); }, [data]);
  const save = async (path: string, body: unknown) => {
    setSaveError(null);
    try { setView(await withTimeout(lab<ScheduleView>(path, { method: 'PUT', body }))); } catch (e) { setSaveError(messageOf(e)); }
  };
  void strategies;
  return (
    <section className="sl-card" aria-label="Scanner schedule">
      <header className="sl-card-head"><h3>Scanner schedule</h3><button type="button" onClick={() => void load()}>Refresh</button></header>
      {error && <Notice tone="bad">Could not load the schedule: {error} <button type="button" className="sl-link" onClick={() => void load()}>Retry</button></Notice>}
      {saveError && <Notice tone="bad">{saveError}</Notice>}
      {!error && view === null && <p className="sl-muted" role="status">Loading schedule… (gives up after 15 s)</p>}
      {view && (
        <>
          <Notice tone={view.scans_enabled ? 'ok' : 'warn'}>
            <label className="sl-check">
              <input type="checkbox" checked={view.scans_enabled} onChange={(e) => void save('/scanner/settings', { scans_enabled: e.target.checked })} />
              {' '}<strong>Master switch: scheduled scanning is {view.scans_enabled ? 'ON' : 'OFF'}</strong>
            </label>
            <p className="sl-muted">{view.note} Off by default. This page's browser tab is not involved: the server scans whether or not it is open.</p>
          </Notice>
          {view.strategies.length === 0 ? <Empty title="No active intraday strategies">Activate a strategy in the Library to schedule it.</Empty> : (
            <Scroll label="Per-strategy schedule">
              <table className="sl-table sl-compact">
                <thead><tr><th>Strategy</th><th>Scan</th><th>Cadence</th><th>Next scheduled scan</th><th>Last note</th></tr></thead>
                <tbody>
                  {view.strategies.map((s) => {
                    const options = [s.min_cadence_minutes, s.min_cadence_minutes * 3, s.min_cadence_minutes * 6, s.min_cadence_minutes * 12].filter((v, i, a) => a.indexOf(v) === i && v <= 240);
                    return (
                      <tr key={s.strategy_id}>
                        <td>{s.name}<div className="sl-muted">{s.market} · decides on {s.decision_timeframe} candles</div></td>
                        <td><label className="sl-check"><input type="checkbox" aria-label={`Schedule ${s.name}`} checked={s.enabled} onChange={(e) => void save(`/scanner/schedule/${s.strategy_id}`, { enabled: e.target.checked, cadence_minutes: s.cadence_minutes })} /> {s.enabled ? 'Enabled' : 'Disabled'}</label></td>
                        <td>
                          <select aria-label={`Cadence for ${s.name}`} value={s.cadence_minutes} onChange={(e) => void save(`/scanner/schedule/${s.strategy_id}`, { enabled: s.enabled, cadence_minutes: Number(e.target.value) })}>
                            {[...new Set([...options, s.cadence_minutes])].sort((a, b) => a - b).map((m) => <option key={m} value={m}>every {m} min (at candle close)</option>)}
                          </select>
                        </td>
                        <td>{s.enabled ? (view.scans_enabled ? fmtIst(s.next_run_at) : <span className="sl-muted">paused by master switch</span>) : '—'}</td>
                        <td className="sl-muted">{s.last_skip_reason ? s.last_skip_reason.replace(/_/g, ' ') : '—'}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </Scroll>
          )}
          <details className="sl-issue-group">
            <summary><strong>Expected provider request volume per scan cycle: {view.request_estimate.provider_requests_per_scan_cycle}</strong></summary>
            <p className="sl-muted">{view.request_estimate.basis}</p>
            <p className="sl-muted">Reference: {view.request_estimate.dhan_documented_limits_for_reference}</p>
            <p className="sl-muted">Scans are only as fresh as the stored 1-minute data; a stale or short series is suppressed with a reason, not scanned.</p>
          </details>
        </>
      )}
    </section>
  );
}

export function ScanHistoryPanel({ onShowRejections }: { onShowRejections: (scanRunId: string) => void }) {
  const { data, error, load } = useLoad<ScanRunView[]>('/scan-runs?limit=30', (runs) => runs.some((r) => r.status === 'running'));
  return (
    <section className="sl-card" aria-label="Scan history">
      <header className="sl-card-head"><h3>Scan history</h3><button type="button" onClick={() => void load()}>Refresh</button></header>
      {error && <Notice tone="bad">Could not load scan history: {error} <button type="button" className="sl-link" onClick={() => void load()}>Retry</button></Notice>}
      {!error && data === null && <p className="sl-muted" role="status">Loading scan history… (gives up after 15 s)</p>}
      {data !== null && data.length === 0 && <Empty title="No scans recorded yet">Press “Scan now” in Live Signals, or enable a schedule.</Empty>}
      {data !== null && data.length > 0 && (
        <Scroll label="Scan runs">
          <table className="sl-table sl-compact">
            <thead><tr><th>Started (IST)</th><th>Trigger</th><th>Status</th><th>Covered</th><th className="sl-r">Signals</th><th>Suppressed</th></tr></thead>
            <tbody>
              {data.map((r) => (
                <tr key={r.scan_run_id}>
                  <td>{fmtIst(r.started_at)}</td><td>{r.trigger === 'schedule' ? 'Scheduled' : r.trigger === 'manual' ? 'Manual' : '—'}</td>
                  <td><Pill tone={r.status === 'completed' ? 'ok' : r.status === 'failed' ? 'bad' : 'info'}>{r.status}</Pill>{r.errors.length > 0 && <div className="sl-muted">{r.errors.length} error{r.errors.length === 1 ? '' : 's'}: {r.errors[0].error}</div>}</td>
                  <td>{r.strategies} strateg{r.strategies === 1 ? 'y' : 'ies'} · {r.instruments_scanned} instrument{r.instruments_scanned === 1 ? '' : 's'} ({r.pairs_scanned} pairs)</td>
                  <td className="sl-r">{r.new_signals}</td>
                  <td>{r.rejections.total ? <button type="button" className="sl-link" onClick={() => onShowRejections(r.scan_run_id)}>{suppressionText(r.rejections)}</button> : '0'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Scroll>
      )}
    </section>
  );
}
