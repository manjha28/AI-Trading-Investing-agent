import { useEffect, useState } from 'react';
import { apiRequest } from '../api';
import { Notice, Pill, Scroll } from './shared';
import { VERIFY_LABEL, VERIFY_TONE, show, type Cfg } from './varFormat';

const SEGMENTS: [string, string, string, string][] = [
  ['index_futures', 'NSE index futures', 'futures', 'index_future'],
  ['stock_futures', 'NSE stock futures', 'futures', 'stock_future'],
  ['equity_cash', 'NSE equity cash (intraday / MIS)', 'equity_cash', 'cash'],
];

export function chargeParams(segment: string): { segment: string; instrument_type: string } {
  const row = SEGMENTS.find((s) => s[0] === segment) ?? SEGMENTS[0];
  return { segment: row[2], instrument_type: row[3] };
}

/** Indian Trading Charges panel: the full applicable schedule. A missing/invalid schedule is shown as unavailable, never as a valid net result. */
export function VarCharges({ segment, asOf, runSchedule }: { segment: string; asOf?: string; runSchedule?: Cfg | null }) {
  const [seg, setSeg] = useState(segment);
  const [sched, setSched] = useState<Cfg | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => setSeg(segment), [segment]);
  useEffect(() => {
    setErr(null); setSched(null);
    const p = chargeParams(seg);
    const q = new URLSearchParams({ segment: p.segment, instrument_type: p.instrument_type, ...(asOf ? { on: asOf } : {}) });
    void apiRequest<Cfg>(`/var/charges/schedule?${q.toString()}`).then(setSched).catch((e: unknown) => setErr(e instanceof Error ? e.message : 'The fee schedule is unavailable.'));
  }, [seg, asOf]);

  const comps = (sched?.components ?? {}) as Record<string, Cfg>;
  const flagged = Object.entries(comps).filter(([, c]) => c.status !== 'verified_primary');
  return (
    <section className="sl-card" aria-label="Indian trading charges">
      <div className="sl-card-head">
        <h2>Indian trading charges</h2>
        <label>Segment <select value={seg} onChange={(e) => setSeg(e.target.value)}>{SEGMENTS.map((s) => <option key={s[0]} value={s[0]}>{s[1]}</option>)}</select></label>
      </div>
      {runSchedule?.unavailable && <Notice tone="bad">This run's fee schedule was unavailable ({String(runSchedule.unavailable)}). Net P&amp;L is NOT valid for this run.</Notice>}
      {err && <Notice tone="bad">Fee schedule unavailable: {err}. Without a valid schedule no net P&amp;L is shown as valid.</Notice>}
      {!sched && !err && <p className="sl-muted">Loading schedule…</p>}
      {sched && (
        <>
          <p className="sl-muted">Broker <strong>{show(sched.broker)}</strong> · exchange <strong>{show(sched.exchange)}</strong> · segment <strong>{show(sched.segment)} / {show(sched.instrument_type)}</strong> · as of {show(sched.as_of)} ·
            schedule version <strong>{show(sched.schedule_version)}</strong> · last verified {show(sched.last_verified)}</p>
          {flagged.length > 0 && <Notice tone="warn">{flagged.length} component(s) are not verified against a primary source: {flagged.map(([k, c]) => `${k} (${VERIFY_LABEL[c.status] ?? c.status})`).join(', ')}. Net figures that depend on them are estimates.</Notice>}
          <Scroll label="Charge schedule">
            <table className="sl-table"><thead><tr><th>Component</th><th>Rate</th><th>Unit</th><th>Effective from</th><th>Status</th><th>Source / note</th></tr></thead>
              <tbody>{Object.entries(comps).map(([k, c]) => (
                <tr key={k}><th scope="row">{k}</th><td>{show(c.rate)}</td><td>{show(c.unit)}</td><td>{show(c.effective_from)}</td>
                  <td><Pill tone={VERIFY_TONE[c.status] ?? 'bad'}>{VERIFY_LABEL[c.status] ?? show(c.status)}</Pill></td>
                  <td className="sl-muted">{c.source ? <a href={String(c.source)} target="_blank" rel="noreferrer">source</a> : '—'} {show(c.note)}</td></tr>))}</tbody></table>
          </Scroll>
          <p className="sl-muted">Brokerage is charged once per executed order (entry, each partial exit and the final exit are separate orders). GST applies to brokerage + exchange + IPFT + SEBI, not to STT or stamp duty.
            Rounding: {show(sched.rounding_method)}.{sched.brokerage_pct_cap ? ` Cash brokerage is the lower of the flat fee and ${sched.brokerage_pct_cap}% of turnover.` : ''}</p>
        </>
      )}
    </section>
  );
}
