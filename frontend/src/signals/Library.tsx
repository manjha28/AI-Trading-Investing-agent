import { useMemo, useState } from 'react';
import { apiRequest } from '../api';
import { lab, fmtPct, fmtTime, messageOf } from './lab';
import { ConfirmDialog, Empty, Notice, Pill, Scroll, StatusPill } from './shared';
import type { Definition, Readiness, RunRow, StrategyRow } from './types';

type Props = {
  strategies: StrategyRow[]; loading: boolean; error: string | null;
  readiness?: Readiness | null; onReload: () => void; onEdit: (s: StrategyRow) => void; onOpenStarter: (s: StrategyRow) => void; onDraft: (d: Definition, text: string | null, note: string) => void; onBacktest: (s: StrategyRow) => void;
};

export function Library({ strategies, loading, error, readiness, onReload, onEdit, onOpenStarter, onDraft, onBacktest }: Props) {
  const [q, setQ] = useState('');
  const [kind, setKind] = useState('all');
  const [market, setMarket] = useState('all');
  const [status, setStatus] = useState('all');
  const [source, setSource] = useState('all');
  const [sel, setSel] = useState<StrategyRow | null>(null);
  const [runs, setRuns] = useState<RunRow[]>([]);
  const [confirm, setConfirm] = useState<{ s: StrategyRow; action: 'activate' | 'suspend' | 'retire' } | null>(null);
  const [dupFor, setDupFor] = useState<StrategyRow | null>(null);
  const [dupName, setDupName] = useState('');
  const [msg, setMsg] = useState<{ tone: 'ok' | 'bad'; text: string } | null>(null);

  const rows = useMemo(() => strategies.filter((s) =>
    (kind === 'all' || s.kind === kind) && (market === 'all' || s.market === market) && (status === 'all' || s.status === status)
    && (source === 'all' || s.source_type === source) && (!q.trim() || s.name.toLowerCase().includes(q.trim().toLowerCase()))), [strategies, q, kind, market, status, source]);

  const open = async (s: StrategyRow) => {
    try {
      setSel(await lab<StrategyRow>(`/strategies/${s.id}`));
      setRuns(s.kind === 'intraday' ? await lab<RunRow[]>(`/backtests?strategy_id=${s.id}`) : []);
    } catch (e) { setMsg({ tone: 'bad', text: messageOf(e) }); }
  };
  const lifecycle = async () => {
    if (!confirm) return;
    const done = { activate: 'activated', suspend: 'suspended', retire: 'retired' }[confirm.action];
    try {
      await apiRequest(`/strategies/${confirm.s.id}/${confirm.action}`, { method: 'POST', body: '{}' });
      setMsg({ tone: 'ok', text: `${confirm.s.name} ${done}.` });
      setConfirm(null);
      onReload();
    } catch (e) { setMsg({ tone: 'bad', text: messageOf(e) }); setConfirm(null); }
  };
  const duplicate = async () => {
    if (!dupFor || !dupName.trim()) return;
    try {
      await lab(`/strategies/${dupFor.id}/duplicate`, { method: 'POST', body: { name: dupName.trim() } });
      setMsg({ tone: 'ok', text: `Duplicated as ${dupName.trim()} (suspended).` });
      setDupFor(null); setDupName('');
      onReload();
    } catch (e) { setMsg({ tone: 'bad', text: messageOf(e) }); }
  };
  const convert = async (s: StrategyRow) => {
    try {
      const r = await lab<{ definition: Definition; note: string }>(`/strategies/${s.id}/convert-draft?decision_timeframe=5m`);
      onDraft(r.definition, null, r.note);
    } catch (e) { setMsg({ tone: 'bad', text: messageOf(e) }); }
  };

  return (
    <div className="sl-library">
      <div className="sl-filters" role="search" aria-label="Strategy filters">
        <input aria-label="Search strategies" placeholder="Search name…" value={q} onChange={(e) => setQ(e.target.value)} />
        <select aria-label="Type" value={kind} onChange={(e) => setKind(e.target.value)}><option value="all">All types</option><option value="intraday">Intraday</option><option value="legacy_daily">Legacy daily</option></select>
        <select aria-label="Market" value={market} onChange={(e) => setMarket(e.target.value)}><option value="all">All markets</option><option value="equity_in">Indian equity</option><option value="crypto">Crypto</option></select>
        <select aria-label="Status" value={status} onChange={(e) => setStatus(e.target.value)}><option value="all">All statuses</option><option value="active">Active</option><option value="suspended">Suspended</option><option value="retired">Retired</option></select>
        <select aria-label="Source" value={source} onChange={(e) => setSource(e.target.value)}><option value="all">All sources</option><option value="visual">Visual</option><option value="pine">Pine</option><option value="python">Python</option><option value="legacy">Legacy</option><option value="starter">Built-in starter</option></select>
        <span className="sl-muted">{rows.length} of {strategies.length}</span>
      </div>
      {strategies.some((x) => x.origin === 'starter') && (
        <Notice tone="info">Built-in starter strategies are ordinary, fully editable strategies with sensible defaults. They have <strong>not been tested</strong>: no performance is shown until you run a real backtest.
          {readiness && <> Data: {Object.entries(readiness).map(([m, r]) => `${m === 'crypto' ? 'crypto' : 'Indian equity'} ${r.backtest_ready ? `${r.instruments_with_1m_data} instrument(s) with 1m data` : 'no 1-minute data stored (not backtest-ready)'}`).join(' · ')}.</>}</Notice>
      )}
      {error && <Notice tone="bad">{error}</Notice>}
      {msg && <Notice tone={msg.tone}>{msg.text}</Notice>}
      {loading && !strategies.length ? <p className="sl-muted">Loading strategies…</p> : !rows.length ? (
        <Empty title={strategies.length ? 'No strategies match these filters' : 'No strategies yet'}>{strategies.length ? 'Clear a filter to see the rest.' : 'Create one in the Strategy Builder or import Pine / Python.'}</Empty>
      ) : (
        <Scroll label="Strategies">
          <table className="sl-table">
            <thead><tr><th>Name</th><th>Type</th><th className="sl-hide-sm">Market</th><th className="sl-hide-sm">TF</th><th>Status</th><th>Ver.</th><th className="sl-hide-sm">Modified</th><th className="sl-hide-sm">Last backtest</th><th>Actions</th></tr></thead>
            <tbody>
              {rows.map((s) => (
                <tr key={s.id} className={sel?.id === s.id ? 'sl-selected' : ''}>
                  <td><button type="button" className="sl-link" onClick={() => open(s)}>{s.name}</button>
                    {s.kind === 'intraday' && <div className="sl-sub">{s.description}{s.indicator_summary?.length ? <span className="sl-summary-line"> · {s.indicator_summary.join(' · ')}</span> : null}</div>}
                    {s.options_summary && (
                      <div className="sl-sub" aria-label="Options summary">
                        {s.options_summary.market} · {s.options_summary.type} · {s.options_summary.directions.join('/')} · base {s.options_summary.base_data} · trend {s.options_summary.trend_timeframe} · max {s.options_summary.max_duration_minutes} min · stop {s.options_summary.premium_stop_pct}% / {s.options_summary.target_r}R
                        {' '}<Pill tone={s.data_coverage?.ready ? (s.data_coverage.mode === 'quote_accurate' ? 'ok' : 'warn') : 'bad'} title={(s.data_coverage?.blockers ?? []).join(' | ')}>{s.data_coverage?.ready ? `data: ${s.data_coverage.mode.replace('_', ' ')}` : 'data coverage: unavailable'}</Pill>
                      </div>)}
                    {s.derived_from && <div className="sl-sub">derived from {s.derived_from.name} v{s.derived_from.version}</div>}</td>
                  <td>{s.origin === 'starter' ? <Pill tone="info" title="Ships with the app; fully editable via Open in Builder">{s.origin_label ?? 'Built-in Starter Strategy'}</Pill> : s.origin === 'customized' ? <Pill tone="info">customized</Pill> : s.kind === 'intraday' ? <Pill tone="info">{s.source_type}</Pill> : <Pill tone="neutral" title="Daily-bar strategy from the original Signals page">legacy daily</Pill>}</td>
                  <td className="sl-hide-sm">{s.market === 'crypto' ? 'Crypto' : 'India eq.'}</td><td className="sl-hide-sm">{s.decision_timeframe}</td>
                  <td><StatusPill status={s.status} /></td><td className="sl-r">v{s.current_version}</td>
                  <td className="sl-hide-sm" title={`Created ${fmtTime(s.created_at)}`}>{fmtTime(s.updated_at)}</td>
                  <td className="sl-hide-sm">{s.last_backtest ? <span title={`v${s.last_backtest.version}`}>{s.last_backtest.trades ?? 0} trades · {fmtPct(s.last_backtest.return_pct)}</span> : s.kind === 'intraday' ? <Pill tone="neutral" title={s.options_summary && !s.data_coverage?.ready ? 'Cannot be backtested: options data is not available' : 'No real backtest has been run for this strategy'}>{s.options_summary && !s.data_coverage?.ready ? 'not tested · data unavailable' : 'not tested'}</Pill> : <span className="sl-muted">—</span>}</td>
                  <td className="sl-actions">
                    <button type="button" onClick={() => open(s)}>Open</button>
                    {s.origin === 'starter'
                      ? <><button type="button" className="sl-primary" onClick={() => onOpenStarter(s)} title="Opens an editable copy in the Strategy Builder; the starter itself is never changed">Open in Builder</button>
                          <button type="button" onClick={() => { setDupFor(s); setDupName(`${s.name.replace(/^Starter: /, '')} (custom)`); }} title="Saves an independent editable copy now">Customize</button></>
                      : s.kind === 'intraday'
                      ? <button type="button" disabled={s.status === 'retired'} onClick={() => onEdit(s)} title={s.status === 'retired' ? 'Retired strategies cannot be versioned: duplicate instead' : 'Creates a new version'}>Edit as new version</button>
                      : <button type="button" onClick={() => convert(s)} title="Creates an unsaved intraday draft; the legacy row is never modified">Convert to intraday draft</button>}
                    {s.origin !== 'starter' && <button type="button" onClick={() => { setDupFor(s); setDupName(`${s.name} copy`); }}>Duplicate</button>}
                    {s.status !== 'active' && <button type="button" disabled={s.status === 'retired'} onClick={() => setConfirm({ s, action: 'activate' })}>Activate</button>}
                    {s.status === 'active' && <button type="button" onClick={() => setConfirm({ s, action: 'suspend' })}>Suspend</button>}
                    <button type="button" className="sl-danger-text" disabled={s.status === 'retired'} onClick={() => setConfirm({ s, action: 'retire' })}>Retire</button>
                    <button type="button" disabled={s.kind !== 'intraday'} title={s.kind !== 'intraday' ? 'Legacy daily strategies are backtested in the Legacy tab' : ''} onClick={() => onBacktest(s)}>Backtest</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </Scroll>
      )}

      {sel && (
        <section className="sl-card" aria-label={`${sel.name} history`}>
          <header className="sl-card-head"><h3>{sel.name} <StatusPill status={sel.status} /></h3><button type="button" onClick={() => setSel(null)}>Close</button></header>
          <p className="sl-muted">{sel.description || 'No description.'}</p>
          {sel.kind === 'intraday' && <p className="sl-muted">Market: {sel.market === 'crypto' ? 'Crypto (24/7)' : 'Indian equity (NSE)'} · evaluation timeframe {sel.decision_timeframe} (change it in the Builder) · {sel.tested ? 'has real backtest results' : 'not tested'}{readiness && !readiness[sel.market]?.backtest_ready ? ' · no stored 1-minute data for this market: not backtest-ready' : ''}</p>}
          {sel.options_summary && sel.data_coverage && (
            <Notice tone={sel.data_coverage.ready ? 'info' : 'warn'}>{sel.data_coverage.ready ? `Options data available (${sel.data_coverage.mode.replace('_', ' ')}).` : `Options backtesting unavailable: ${sel.data_coverage.blockers.join(' ')}`}</Notice>
          )}
          {sel.indicator_summary && sel.indicator_summary.length > 0 && <p className="sl-muted">Indicators: {sel.indicator_summary.join(', ')}</p>}
          <h4>Version history</h4>
          <Scroll label="Versions">
            <table className="sl-table sl-compact"><thead><tr><th>Version</th><th>Created</th><th>Source</th><th>Hash</th><th>Locked by backtest</th><th>Detail</th></tr></thead><tbody>
              {sel.versions?.map((v) => (
                <tr key={v.version}><td>v{v.version}{v.version === sel.current_version && ' (current)'}</td><td>{fmtTime(v.created_at)}</td><td>{v.source_type}{v.source_filename ? ` · ${v.source_filename}` : ''}</td>
                  <td><code>{v.definition_hash?.slice(0, 12) ?? '—'}</code></td><td>{v.locked ? 'yes' : 'no'}</td>
                  <td>{v.definition ? `${v.definition.indicators.length} indicators · ${v.definition.decision_timeframe} · ${v.definition.universe.length} instrument(s)` : <code>{JSON.stringify(v.params)}</code>}</td></tr>
              ))}
            </tbody></table>
          </Scroll>
          {sel.kind === 'intraday' && (
            <>
              <h4>Backtest history</h4>
              {runs.length ? <Scroll label="Backtests"><table className="sl-table sl-compact"><thead><tr><th>When</th><th>Ver.</th><th>Kind</th><th>Status</th><th>Trades</th><th>Net</th><th>Return</th></tr></thead><tbody>
                {runs.map((r) => <tr key={r.id}><td>{fmtTime(r.created_at)}</td><td>v{r.strategy_version}</td><td>{r.kind}</td><td><StatusPill status={r.status} /></td><td className="sl-r">{String(r.metrics?.total_trades ?? '—')}</td><td className="sl-r">{String(r.metrics?.net_profit ?? '—')}</td><td className="sl-r">{r.metrics ? fmtPct(r.metrics.return_pct as number) : '—'}</td></tr>)}
              </tbody></table></Scroll> : <p className="sl-muted">No backtests yet for this strategy.</p>}
            </>
          )}
        </section>
      )}

      {confirm && (
        <ConfirmDialog title={`${confirm.action[0].toUpperCase()}${confirm.action.slice(1)} “${confirm.s.name}”?`} confirmLabel={confirm.action[0].toUpperCase() + confirm.action.slice(1)} danger={confirm.action === 'retire'}
          onCancel={() => setConfirm(null)} onConfirm={lifecycle}
          body={confirm.action === 'retire' ? <p>Retiring is permanent: a retired strategy can no longer be activated, versioned or changed. Its history, signals and backtests are kept. You can still duplicate it.</p>
            : confirm.action === 'activate' ? <p>Active strategies are scanned for signals. Signals are decision support only and never place orders.</p> : <p>Suspended strategies are not scanned. Existing history is kept.</p>} />
      )}
      {dupFor && (
        <ConfirmDialog title={`Duplicate “${dupFor.name}”`} confirmLabel="Duplicate" onCancel={() => setDupFor(null)} onConfirm={duplicate}
          body={<label className="sl-field"><span>Name for your copy</span><input autoFocus value={dupName} onChange={(e) => setDupName(e.target.value)} maxLength={120} /></label>} />
      )}
    </div>
  );
}
