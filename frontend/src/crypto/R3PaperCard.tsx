import { useEffect, useState } from 'react';
import { apiRequest } from '../api';
import { messageOf } from './cryptoApi';

type Trade = { timestamp_utc: string; symbol: string; side: string; intended_entry_ref: string; exit_time_utc: string; exit_reason: string; net_usd: string; net_bps: string };
type Status = {
  stale: boolean; error: string | null; fetched_at: string | null; started?: string | null; param_hash?: string; paper_heartbeat_age_s?: number | null;
  recorder?: { running: boolean; uptime_pct_24h: number | null; last_snapshot: string | null };
  symbols?: Record<string, { open: number | null; closed: number | null; last_bar: string | null }>;
  trades?: Trade[]; closed_trades?: number; net_usd_total?: string;
  checkpoints?: { trade30: { net_bps_per_trade: number; profit_factor: number | null } | null; holdout_reference_net_bps: number };
  go_no_go?: { decided: boolean; reason?: string; passed?: boolean }; label?: string;
};

const when = (iso?: string | null) => (iso ? iso.replace('T', ' ').slice(0, 16) + ' UTC' : '—');

/** The frozen R3 paper test (1-hour range breakout, 3R target, 48 h time stop) that runs on the separate lab machine. Paper only. */
export function R3PaperCard() {
  const [s, setS] = useState<Status | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    const load = () => apiRequest<Status>('/crypto-delta/r3-paper').then((r) => { if (live) { setS(r); setError(null); } }).catch((e: unknown) => { if (live) setError(messageOf(e)); });
    void load();
    const timer = window.setInterval(() => { if (document.visibilityState === 'visible') void load(); }, 60_000);
    return () => { live = false; window.clearInterval(timer); };
  }, []);
  const n = s?.closed_trades ?? 0;
  return (
    <section className="crypto-forward-detail" aria-label="R3 paper test">
      <h3>R3 hourly breakout <span className="chip">Paper only</span></h3>
      <p className="crypto-sub">BTCUSD and ETHUSD. When the last 24 hours are among the quietest 10% of the past 60 days, it sets a stop-entry on both sides of that range: target 3R, stop at the other side, out after 48 h at the latest. Rules frozen (hash {s?.param_hash ?? '—'}). Usually about one trade a week per coin. Hypothetical results, never real money.</p>
      {error && <p role="alert" className="crypto-error">{error}</p>}
      {s?.stale && <p role="alert" className="crypto-error">Stale: {s.error}. {s.fetched_at ? `Showing the copy from ${when(s.fetched_at)}.` : 'No copy yet.'}</p>}
      {!s && !error && <p className="crypto-sub" role="status" aria-busy="true">Loading from the lab machine…</p>}
      {s && s.started !== undefined && <>
        <dl className="crypto-kpis" aria-label="R3 status">
          <div><dt>Running since</dt><dd>{when(s.started)}</dd></div>
          <div><dt>Engine</dt><dd>{s.paper_heartbeat_age_s == null ? '—' : s.paper_heartbeat_age_s < 180 ? 'alive' : `silent ${Math.round(s.paper_heartbeat_age_s / 60)} min`}</dd></div>
          <div><dt>Order-book recorder</dt><dd>{s.recorder?.running ? `up · ${s.recorder.uptime_pct_24h ?? '—'}% (24 h)` : 'down'}</dd></div>
          <div><dt>Closed paper trades</dt><dd>{n} of 50</dd></div>
          <div><dt>Paper net (USD)</dt><dd>{s.net_usd_total ?? '—'}</dd></div>
          <div><dt>Open now</dt><dd>{Object.entries(s.symbols ?? {}).map(([k, v]) => `${k} ${v.open ?? 0}`).join(' · ') || '—'}</dd></div>
        </dl>
        <p className="crypto-sub">Checkpoints: first comparison with the backtest at 30 trades (backtest reference {s.checkpoints?.holdout_reference_net_bps?.toFixed(1) ?? '—'} net bps per trade){s.checkpoints?.trade30 ? `: now ${s.checkpoints.trade30.net_bps_per_trade.toFixed(1)} bps` : ''}; go / no-go for real money at 50 trades{s.go_no_go?.decided ? (s.go_no_go.passed ? ': PASSED' : ': not passed') : ` (${s.go_no_go?.reason ?? ''})`}.</p>
        {n > 0 ? (
          <table className="crypto-table"><thead><tr><th>Entry</th><th>Symbol</th><th>Side</th><th>Entry ref</th><th>Exit</th><th>Why</th><th>Net bps</th><th>Net USD</th></tr></thead>
            <tbody>{(s.trades ?? []).slice().reverse().map((t, i) => <tr key={i}><td>{when(t.timestamp_utc)}</td><td>{t.symbol}</td><td>{t.side}</td><td>{t.intended_entry_ref}</td><td>{when(t.exit_time_utc)}</td><td>{t.exit_reason}</td><td>{t.net_bps}</td><td>{t.net_usd}</td></tr>)}</tbody></table>
        ) : <p className="crypto-sub">No paper trade yet: no quiet-range breakout since the test started.</p>}
        <p className="crypto-sub">{s.label}</p>
      </>}
    </section>
  );
}
