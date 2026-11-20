import { Empty, Notice, Pill, Scroll } from './shared';
import { money, num, show, type Cfg } from './varFormat';

function Stat({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return <div className="sl-stat" title={hint}><span className="sl-muted">{label}</span><strong>{value}</strong></div>;
}

const ci = (s?: Cfg) => (s?.ci95 ? `${money(s.ci95[0])} to ${money(s.ci95[1])}` : '—');

/** Performance dashboard. NET is primary. Below 100 trades no performance claim is made: an explicit insufficient-sample state replaces the figures. */
export function VarPerformance({ result, scheduleOk }: { result: Cfg; scheduleOk: boolean }) {
  const s = result.summary as Cfg | undefined;
  if (!s) return <Empty title="No performance yet">Run a backtest first.</Empty>;
  const sample = s.sample as Cfg;
  const insufficient = sample?.state !== 'ok';
  const d = (insufficient ? s.diagnostics_only : s) as Cfg | undefined;
  return (
    <section className="sl-card" aria-label="Performance">
      <div className="sl-card-head"><h2>Performance <Pill tone="warn">HYPOTHETICAL</Pill></h2><span className="sl-muted">Net is primary · configurations evaluated in this run: {show(s.configurations_evaluated)}</span></div>
      {!scheduleOk && <Notice tone="bad">Fee schedule unavailable for this run: net P&amp;L is not valid and is not shown as a result.</Notice>}
      {insufficient ? (
        <Notice tone="warn"><strong>Insufficient sample.</strong> {show(sample?.message ?? `${show(s.trades)} trades`)} No win rate, profit factor, expectancy or P&amp;L claim is made. {show(s.trades)} trades cannot distinguish skill from luck.</Notice>
      ) : (
        <>
          <div className="sl-stats">
            <Stat label="Net P&L (primary)" value={scheduleOk ? money(s.net_pnl) : '—'} />
            <Stat label="Gross P&L" value={money(s.gross_pnl)} />
            <Stat label="Total charges" value={money(s.total_charges)} />
            <Stat label="Trades" value={show(s.trades)} />
            <Stat label="Win rate (net)" value={s.win_rate_net_pct == null ? '—' : `${num(s.win_rate_net_pct, 1)}%`} />
            <Stat label="Net profit factor" value={num(s.net_profit_factor)} />
            <Stat label="Gross profit factor" value={num(s.gross_profit_factor)} />
            <Stat label="Net expectancy / trade" value={money(s.net_expectancy_per_trade)} />
            <Stat label="Gross expectancy / trade" value={money(s.gross_expectancy_per_trade)} />
            <Stat label="Max drawdown (net)" value={money(s.max_drawdown_net)} />
            <Stat label="Turnover" value={money(s.total_turnover)} />
            <Stat label="Avg hold (min)" value={num(s.avg_hold_minutes, 1)} />
          </div>
          <h3>Statistical significance (net P&amp;L per trade)</h3>
          <Scroll label="Statistics"><table className="sl-table"><tbody>
            <tr><th scope="row">Mean per trade</th><td>{money(s.net_per_trade_stats?.mean)}</td></tr>
            <tr><th scope="row">Standard error</th><td>{money(s.net_per_trade_stats?.se)}</td></tr>
            <tr><th scope="row">t-statistic</th><td>{num(s.net_per_trade_stats?.t_stat)}</td></tr>
            <tr><th scope="row">95% confidence interval</th><td>{ci(s.net_per_trade_stats)}</td></tr>
            <tr><th scope="row">Trades</th><td>{show(s.net_per_trade_stats?.n)}</td></tr>
          </tbody></table></Scroll>
          <p className="sl-muted">An interval that includes zero means the mean is not distinguishable from no edge. Many configurations tried in one run inflate the best result; the count is shown above.</p>
          {s.costs_verified === false && <Notice tone="warn">Some charge rates are not verified against a primary source; net figures are estimates.</Notice>}
        </>
      )}
      {d && insufficient && (
        <details><summary>Diagnostics only (not a performance claim)</summary>
          <p className="sl-muted">Accounting figures for audit. With {show(s.trades)} trades these say nothing about the strategy.</p>
          <Scroll label="Diagnostics"><table className="sl-table"><tbody>
            <tr><th scope="row">Gross P&amp;L</th><td>{money(d.gross_pnl)}</td></tr><tr><th scope="row">Total charges</th><td>{money(d.total_charges)}</td></tr>
            <tr><th scope="row">Net P&amp;L</th><td>{scheduleOk ? money(d.net_pnl) : '—'}</td></tr><tr><th scope="row">Turnover</th><td>{money(d.total_turnover)}</td></tr></tbody></table></Scroll></details>
      )}
      {(['by_setup', 'by_direction', 'by_instrument'] as const).map((g) => (
        <div key={g}><h3>{g === 'by_setup' ? 'By engine' : g === 'by_direction' ? 'Long vs short' : 'By instrument'}</h3>
          <Scroll label={g}><table className="sl-table"><thead><tr><th /><th>Trades</th><th>Net P&amp;L</th><th>Net PF</th><th>Net expectancy</th><th>Sample</th></tr></thead>
            <tbody>{Object.entries((result[g] ?? {}) as Record<string, Cfg>).map(([k, v]) => {
              const ok = v.sample?.state === 'ok';
              return <tr key={k}><td>{k}</td><td>{show(v.trades)}</td><td>{ok ? money(v.net_pnl) : '—'}</td><td>{ok ? num(v.net_profit_factor) : '—'}</td><td>{ok ? money(v.net_expectancy_per_trade) : '—'}</td><td>{ok ? 'ok' : <span className="sl-muted">insufficient (&lt; 100)</span>}</td></tr>;
            })}</tbody></table></Scroll></div>
      ))}
    </section>
  );
}
