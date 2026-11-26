import { useSlow } from '../hooks/useSlow';
import { fmtPct, fmtTime } from './lab';
import { LifecycleChip } from './LifecycleChip';
import { BackfillPanel } from './BackfillPanel';
import { Empty, Notice, Pill, Scroll, Stat, StatusPill } from './shared';
import type { Health } from './types';

const REFUSE_PCT = 5;

export function Dashboard({ health, loading, error, goto, reload }: { health: Health | null; loading: boolean; error: string | null; goto: (tab: string) => void; reload?: () => void }) {
  const slow = useSlow(!health && !error);
  if (error) return <Notice tone="bad">{error}</Notice>;
  // health is fetched after the workspace itself, so "no health yet" always means "still loading" (a failed fetch shows its error above), never "no data"
  if (!health) return <p className="sl-muted" role="status" aria-busy="true">{slow ? 'Still loading (slow backend). This can take up to a minute…' : loading ? 'Loading dashboard…' : 'Loading data health…'}</p>;
  const bad = health.data_health.filter((h) => h.status !== 'ok');
  const windowDays = health.data_health.find((h) => h.window_days != null)?.window_days ?? 5;
  return (
    <div className="sl-dashboard">
      {(health.counts.active === 0 || health.recent_backtests.length === 0) && (
        <section className="sl-card sl-next" aria-label="Getting started">
          <header className="sl-card-head"><h3>Getting started</h3></header>
          <ol>
            <li className={health.counts.total ? 'done' : ''}><span>Get a strategy</span><button type="button" onClick={() => goto('registry')}>Pick a ready one</button><button type="button" onClick={() => goto('builder')}>Build your own</button></li>
            <li className={health.recent_backtests.length ? 'done' : ''}><span>Test it on past prices</span><button type="button" onClick={() => goto('backtesting')}>Run a backtest</button></li>
            <li><span>Try it on live prices with paper money</span><button type="button" onClick={() => goto('forward')}>Start a forward test</button></li>
            <li className={health.counts.active ? 'done' : ''}><span>Switch it on to get signals</span><button type="button" onClick={() => goto('live')}>Open live signals</button></li>
          </ol>
        </section>
      )}
      <div className="sl-stats" aria-label="Strategy counts">
        <Stat label="Total strategies" value={health.counts.total} /><Stat label="Active" value={health.counts.active} tone={health.counts.active ? 'pos' : undefined} />
        <Stat label="Suspended" value={health.counts.suspended} /><Stat label="Retired" value={health.counts.retired} />
        <Stat label="Data warnings" value={bad.length} tone={bad.length ? 'neg' : undefined} hint="Instruments used by your strategies whose recent 1-minute data is missing, stale or has gaps" />
      </div>
      {bad.length > 0 && <Notice tone="warn">{bad.length} instrument(s) have 1-minute data problems. Backtests refuse severely incomplete data and live signals are suppressed when data is stale.</Notice>}
      <div className="sl-grid-2">
        <section className="sl-card"><header className="sl-card-head"><h3>Latest signals</h3><button type="button" onClick={() => goto('live')}>Open</button></header>
          {health.recent_signals.length ? <Scroll label="Latest signals"><table className="sl-table sl-compact"><thead><tr><th>Time</th><th>Instrument</th><th>Strategy</th><th>Dir.</th><th>Status</th></tr></thead><tbody>
            {health.recent_signals.map((s) => <tr key={s.id}><td>{fmtTime(s.signal_time)}</td><td>{s.symbol}</td><td>{s.strategy_name} v{s.strategy_version}</td><td className={s.direction === 'long' ? 'sl-pos' : 'sl-neg'}>{s.direction}</td><td><LifecycleChip status={s.status} /></td></tr>)}
          </tbody></table></Scroll> : <Empty title="No signals yet">Activate a strategy and scan from Live Signals.</Empty>}
        </section>
        <section className="sl-card"><header className="sl-card-head"><h3>Recent backtests</h3><button type="button" onClick={() => goto('backtesting')}>Open</button></header>
          {health.recent_backtests.length ? <Scroll label="Recent backtests"><table className="sl-table sl-compact"><thead><tr><th>When</th><th>Kind</th><th>Status</th><th>Trades</th><th>Return</th></tr></thead><tbody>
            {health.recent_backtests.map((r) => <tr key={r.id}><td>{fmtTime(r.created_at)}</td><td>{r.kind}</td><td><StatusPill status={r.status} /></td><td className="sl-r">{String(r.metrics?.total_trades ?? '—')}</td><td className="sl-r">{r.metrics ? fmtPct(r.metrics.return_pct as number) : '—'}</td></tr>)}
          </tbody></table></Scroll> : <Empty title="No backtests yet">Run one under Backtest.</Empty>}
        </section>
      </div>
      <section className="sl-card"><header className="sl-card-head"><h3>Market-data health (1-minute)</h3></header>
        <p className="sl-muted">Recent health is measured over the <strong>last {windowDays} days</strong> only. Stored history is everything saved for the instrument, whatever its age. Provider-unserved minutes are excluded from the missing share (the {REFUSE_PCT}% refusal limit is unchanged) and listed below.</p>
        {health.data_health.length ? <Scroll label="Market data health"><table className="sl-table sl-compact"><thead><tr><th>Instrument</th><th>Market</th><th>Status (last {windowDays}d)</th><th>Bars (last {windowDays}d)</th><th>Last bar age</th><th>Missing (last {windowDays}d)</th><th>Stored history (all time)</th><th>Issues</th></tr></thead><tbody>
          {health.data_health.map((h) => <tr key={h.instrument_id}><td>{h.symbol}{h.provider_coverage && <small className="sl-muted" style={{ display: 'block' }}>{h.market === 'equity_in' ? `09:15-${h.provider_coverage.last_served ?? '?'} available from ${h.provider_coverage.provider} · ` : ''}{h.provider_coverage.start}-{h.provider_coverage.end} not served by provider</small>}</td><td>{h.market}</td><td><StatusPill status={h.status} /></td><td className="sl-r">{h.bars ?? 0}</td>
            <td className="sl-r">{h.last_bar_age_seconds != null ? `${Math.round(h.last_bar_age_seconds / 60)} min` : '—'}</td><td className="sl-r">{h.missing_pct != null ? `${h.missing_pct}%` : '—'}{h.provider_unserved && <small className="sl-muted" style={{ display: 'block' }}>excludes {h.provider_unserved.excluded_minutes} provider-unserved min</small>}</td>
            <td className="sl-r">{h.total_bars != null ? `${h.total_bars.toLocaleString()} bars` : '—'}{h.history_start && h.history_end && <small className="sl-muted" style={{ display: 'block' }}>{h.history_start.slice(0, 10)} → {h.history_end.slice(0, 10)}</small>}</td>
            <td>{h.issues.join('; ') || <Pill tone="ok">clean</Pill>}</td></tr>)}
        </tbody></table></Scroll> : <Empty title="No instruments in use">Strategies' universes appear here.</Empty>}
      </section>
      <BackfillPanel rows={health.data_health} onFinished={() => reload?.()} />
    </div>
  );
}
