import { useCallback, useEffect, useMemo, useState } from 'react';
import { lab, messageOf } from './lab';
import { fmtIst, reasonLabel, withTimeout } from './lifecycle';
import { Empty, Notice, Pill, Scroll } from './shared';
import type { InstrumentRow, RejectionRow, StrategyRow } from './types';

/** Suppressed scan candidates: why a strategy produced no signal for an instrument. Grouped counts by reason double as the filter. */
export function RejectionsPanel({ strategies, instruments, scanRunId }: { strategies: StrategyRow[]; instruments: InstrumentRow[]; scanRunId: string | null }) {
  const [rows, setRows] = useState<RejectionRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [reason, setReason] = useState('all');
  const [run, setRun] = useState(scanRunId ?? 'all');
  useEffect(() => { setRun(scanRunId ?? 'all'); }, [scanRunId]);

  const load = useCallback(async () => {
    setError(null); setRows(null);
    try { setRows(await withTimeout(lab<RejectionRow[]>(`/signals/rejections?limit=300${run !== 'all' ? `&scan_run_id=${run}` : ''}`))); } catch (e) { setError(messageOf(e)); }
  }, [run]);
  useEffect(() => { void load(); }, [load]);

  const strategyName = useMemo(() => new Map(strategies.map((s) => [s.id, s.name])), [strategies]);
  const symbol = useMemo(() => new Map(instruments.map((i) => [i.id, i.symbol])), [instruments]);
  const counts = useMemo(() => {
    const m: Record<string, number> = {};
    for (const r of rows ?? []) m[r.reason_code] = (m[r.reason_code] ?? 0) + 1;
    return Object.entries(m).sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
  }, [rows]);
  const runs = useMemo(() => [...new Set([...(scanRunId ? [scanRunId] : []), ...(rows ?? []).map((r) => r.scan_run_id).filter((x): x is string => !!x)])], [rows, scanRunId]);
  const shown = (rows ?? []).filter((r) => reason === 'all' || r.reason_code === reason);

  return (
    <section className="sl-card" id="sl-rejections" aria-label="Suppressed candidates">
      <header className="sl-card-head"><h3>Suppressed candidates</h3></header>
      <p className="sl-muted">A suppressed candidate is a strategy/instrument pair the scan could not evaluate safely (data quality), so no signal was produced. Times are IST.</p>
      <div className="sl-filters">
        <select aria-label="Filter by reason" value={reason} onChange={(e) => setReason(e.target.value)}>
          <option value="all">All reasons</option>{counts.map(([code, n]) => <option key={code} value={code}>{reasonLabel(code)} ({n})</option>)}
        </select>
        <select aria-label="Filter by scan run" value={run} onChange={(e) => setRun(e.target.value)}>
          <option value="all">All scan runs</option>{runs.map((id) => <option key={id} value={id}>Run {id.slice(0, 8)}</option>)}
        </select>
      </div>
      {counts.length > 0 && <p aria-label="Counts by reason">{counts.map(([code, n]) => <Pill key={code} tone="warn" title={code}>{reasonLabel(code)} {n}</Pill>)}</p>}
      {error && <Notice tone="bad">Could not load rejections: {error} <button type="button" className="sl-link" onClick={() => void load()}>Retry</button></Notice>}
      {!error && rows === null && <p className="sl-muted" role="status">Loading rejections… (gives up after 15 s)</p>}
      {rows !== null && !shown.length && <Empty title={rows.length ? 'No rejections match this filter' : 'No suppressed candidates'}>{rows.length ? 'Choose “All reasons” to see the rest.' : 'Every scanned candidate in this selection was evaluated.'}</Empty>}
      {shown.length > 0 && (
        <Scroll label="Rejections">
          <table className="sl-table sl-compact">
            <thead><tr><th>Instrument</th><th>Strategy version</th><th>Reason</th><th>Detail</th><th>Market data as-of</th><th>Recorded</th><th>Scan run</th></tr></thead>
            <tbody>
              {shown.map((r) => (
                <tr key={r.id}>
                  <td>{symbol.get(r.instrument_id) ?? '—'}</td>
                  <td>{strategyName.get(r.strategy_id) ?? '—'} <span className="sl-muted">{r.strategy_version != null ? `v${r.strategy_version}` : 'v—'}</span></td>
                  <td>{reasonLabel(r.reason_code)}<div><code>{r.reason_code}</code></div></td>
                  <td>{r.reason_detail ?? '—'}</td>
                  <td>{r.observed_data_time ? fmtIst(r.observed_data_time) : '—'}</td>
                  <td>{fmtIst(r.recorded_at)}</td>
                  <td>{r.scan_run_id ? <code>{r.scan_run_id.slice(0, 8)}</code> : <span title="Migrated from a legacy suppressed signal">—</span>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Scroll>
      )}
    </section>
  );
}
