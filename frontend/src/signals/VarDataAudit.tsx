import { Notice, Pill, Scroll } from './shared';
import { show, type Cfg } from './varFormat';

const REASON: Record<string, string> = {
  truncated_or_incomplete_session: 'truncated / incomplete session (fewer than 73 complete 5-minute bars; Dhan serves 1-minute candles only to 15:14 for most windows)',
  off_tick_grid: 'prices off the instrument tick grid (tick size changed or master is wrong)',
};

/** Sessions available vs loaded vs excluded, per symbol, with reasons. Rendered BEFORE any performance panel so an empty or thin run is never mistaken for a result. */
export function VarDataAudit({ audit, status, reason }: { audit: Cfg[] | undefined; status?: string; reason?: string }) {
  const rows = audit ?? [];
  const loaded = rows.reduce((n, a) => n + Number(a.sessions_loaded ?? 0), 0);
  const inWindow = rows.reduce((n, a) => n + Number(a.sessions_loaded_in_window ?? 0), 0);
  const withBaseline = rows.filter((a) => a.rvol_baseline?.can_complete).length;
  return (
    <section className="sl-card" aria-label="Data and sessions">
      <div className="sl-card-head"><h2>Data &amp; sessions</h2>
        <span>{loaded === 0 ? <Pill tone="bad">no usable sessions</Pill> : <Pill tone={inWindow < 21 ? 'warn' : 'ok'}>{inWindow} sessions loaded in window</Pill>}</span></div>
      {status === 'no_usable_data' && <Notice tone="bad"><strong>No usable data.</strong> {reason ?? 'No session could be loaded.'} No performance is shown because nothing was replayed.</Notice>}
      {status === 'order_flow_unavailable' && <Notice tone="bad"><strong>Order flow unavailable.</strong> {reason} No performance is shown.</Notice>}
      {rows.length === 0 ? <p className="sl-muted">No per-symbol data audit was recorded for this run.</p> : (
        <>
          <Scroll label="Per-symbol sessions">
            <table className="sl-table"><thead><tr><th>Symbol</th><th>Available</th><th>Loaded</th><th>Loaded in window</th><th>Excluded</th><th>Why excluded</th><th>First → last loaded</th><th>Rel-volume baseline</th></tr></thead>
              <tbody>{rows.map((a) => (
                <tr key={String(a.underlying)}><th scope="row">{String(a.underlying)}</th><td>{show(a.sessions_available)}</td><td>{show(a.sessions_loaded)}</td><td>{show(a.sessions_loaded_in_window)}</td><td>{show(a.sessions_excluded)}</td>
                  <td>{Object.keys(a.excluded_by_reason ?? {}).length ? Object.entries(a.excluded_by_reason as Record<string, number>).map(([k, v]) => <div key={k}>{v} × {REASON[k] ?? k}</div>) : (a.notes?.[0] ?? '—')}</td>
                  <td>{a.first_loaded ? `${a.first_loaded} → ${a.last_loaded}` : '—'}</td>
                  <td>{a.rvol_baseline?.can_complete ? `from ${a.rvol_baseline.first_session_with_baseline} (${a.rvol_baseline.window_sessions_with_baseline} window sessions)` : <Pill tone="bad">cannot complete</Pill>}</td></tr>))}</tbody></table>
          </Scroll>
          {withBaseline === 0 && loaded > 0 && <Notice tone="warn">The 20-session relative-volume baseline cannot complete for any symbol, so every breakout candidate is rejected for lack of a baseline.</Notice>}
          <p className="sl-muted">History before the start date is loaded (45 calendar days) so the first traded session already has a warm baseline.</p>
        </>
      )}
    </section>
  );
}
