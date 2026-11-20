import { useCallback, useEffect, useMemo, useState } from 'react';
import { apiRequest } from '../api';
import { Empty, Notice, Pill, Scroll, StatusPill } from './shared';
import { VarCharges } from './VarCharges';
import { VarDataAudit } from './VarDataAudit';
import { VarFunnel } from './VarFunnel';
import { VarLedger } from './VarLedger';
import { VarPerformance } from './VarPerformance';
import { money, show, type Cfg, type Run } from './varFormat';

type Caps = {
  futures_candles_1m: { underlying: string; type: string; contracts: number; bars: number; first: string; last: string; sessions: number }[];
  stock_futures: { underlyings_with_candles: number; bars: number };
  data_level: string; supports: string[]; does_not_support: string[]; order_flow: string; volume_unit: string; tick_size_unit: string; lot_size_note: string; session_note: string;
};

const SETTINGS: { path: [string, string]; label: string; step: number }[] = [
  { path: ['risk', 'risk_per_trade_pct'], label: 'Risk per trade (% of start-of-day equity)', step: 0.05 },
  { path: ['risk', 'max_daily_loss_pct'], label: 'Daily loss limit (%)', step: 0.05 },
  { path: ['risk', 'max_entries_per_session'], label: 'Max entries per session', step: 1 },
  { path: ['risk', 'min_net_reward_to_cost'], label: 'Net reward ÷ est. charges, k (net/C ≥ k)', step: 0.5 },
  { path: ['market_structure', 'value_area_pct'], label: 'Value area %', step: 1 },
  { path: ['breakout', 'minimum_outside_volume_pct'], label: 'Breakout: volume beyond level %', step: 1 },
  { path: ['breakout', 'delta_ratio_minimum'], label: 'Breakout delta ratio (min)', step: 0.01 },
  { path: ['breakout', 'relative_volume_minimum'], label: 'Breakout relative volume (min)', step: 0.1 },
  { path: ['breakout', 'minimum_rr'], label: 'Breakout min R:R', step: 0.1 },
  { path: ['reversal', 'rejection_delta_ratio'], label: 'Reversal delta ratio (min)', step: 0.01 },
  { path: ['reversal', 'minimum_rr'], label: 'Reversal min R:R', step: 0.1 },
  { path: ['regime', 'slope_threshold'], label: 'VWAP slope threshold (ATR)', step: 0.01 },
  { path: ['exits', 'partial_booking_pct'], label: 'Partial booking %', step: 5 },
  { path: ['exits', 'time_stop_bars'], label: 'Time stop (5m bars)', step: 1 },
];
const SUBTABS = [['overview', 'Overview'], ['config', 'Config & run'], ['charges', 'Charges'], ['funnel', 'Candidate funnel'], ['performance', 'Performance'], ['ledger', 'Trade ledger']] as const;
type Sub = (typeof SUBTABS)[number][0];

export function VarIntraday() {
  const [sub, setSub] = useState<Sub>('overview');
  const [caps, setCaps] = useState<Caps | null>(null);
  const [cfg, setCfg] = useState<Cfg | null>(null);
  const [version, setVersion] = useState(0);
  const [runs, setRuns] = useState<Run[]>([]);
  const [open, setOpen] = useState<Run | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [segment, setSegment] = useState('index_futures');
  const [unders, setUnders] = useState('NIFTY, BANKNIFTY');
  const [capital, setCapital] = useState('2000000');
  const [start, setStart] = useState('');
  const [end, setEnd] = useState('');
  const [topN, setTopN] = useState('');
  const [pre, setPre] = useState<Cfg[] | null>(null);
  const [allowPartial, setAllowPartial] = useState(false);

  const loadRuns = useCallback(async () => {
    try { setRuns(await apiRequest<Run[]>('/var/backtests')); } catch (e) { setError(e instanceof Error ? e.message : 'Runs could not be loaded.'); }
  }, []);

  useEffect(() => {
    void (async () => {
      try {
        setCaps(await apiRequest<Caps>('/var/data-capabilities'));
        const c = await apiRequest<{ config: Cfg; version_no: number }>('/var/config');
        setCfg(c.config); setVersion(c.version_no);
      } catch (e) { setError(e instanceof Error ? e.message : 'The VAR workspace could not be loaded.'); }
      await loadRuns();
    })();
  }, [loadRuns]);

  // Poll only while a run is queued or running; nothing here polls market data.
  const active = runs.some((r) => r.status === 'queued' || r.status === 'running');
  useEffect(() => {
    if (!active) return undefined;
    const t = window.setInterval(() => void loadRuns(), 3000);
    return () => window.clearInterval(t);
  }, [active, loadRuns]);

  const setField = (sec: string, key: string, raw: string) => setCfg((c) => (c ? { ...c, [sec]: { ...c[sec], [key]: raw === '' ? '' : Number(raw) } } : c));
  const toggleEngine = (name: string) => setCfg((c) => {
    if (!c) return c;
    const cur: string[] = c.engines ?? ['breakout', 'reversal'];
    const next = cur.includes(name) ? cur.filter((x) => x !== name) : [...cur, name];
    return { ...c, engines: next.length ? next : cur };
  });

  const save = async () => {
    if (!cfg) return;
    setError(null); setNotice(null);
    try {
      const r = await apiRequest<{ version_no: number }>('/var/config', { method: 'PUT', body: JSON.stringify(cfg) });
      setVersion(r.version_no); setNotice(`Saved configuration version ${r.version_no}. Completed backtests keep the configuration they ran with.`);
    } catch (e) { setError(e instanceof Error ? e.message : 'The configuration was rejected.'); }
  };

  const names = () => unders.split(',').map((x) => x.trim()).filter(Boolean);
  const check = async () => {
    setError(null); setNotice(null);
    try {
      const q = new URLSearchParams({ symbols: names().join(','), segment, ...(start ? { start } : {}), ...(end ? { end } : {}) });
      setPre((await apiRequest<{ symbols: Cfg[] }>(`/var/preflight?${q.toString()}`)).symbols);
    } catch (e) { setError(e instanceof Error ? e.message : 'The data check failed.'); }
  };
  useEffect(() => { setPre(null); setAllowPartial(false); }, [segment, unders, start, end]);
  const usable = (pre ?? []).filter((p) => p.status === 'ok').length;
  const unusable = (pre ?? []).length - usable;

  const launch = async () => {
    setError(null); setNotice(null);
    try {
      await apiRequest('/var/backtests', { method: 'POST', body: JSON.stringify({
        underlyings: names(), initial_capital: capital, start: start || null, end: end || null, config: cfg, segment,
        broker_profile: 'dhan', exchange: 'NSE', stock_universe_top_n: topN ? Number(topN) : null, allow_partial: allowPartial }) });
      setNotice('Backtest queued. It runs in the background on stored candles.'); await loadRuns();
    } catch (e) { setError(e instanceof Error ? e.message : 'The backtest could not be queued.'); }
  };

  const view = async (id: string, to?: Sub) => {
    try { setOpen(await apiRequest<Run>(`/var/backtests/${id}`)); if (to) setSub(to); } catch (e) { setError(e instanceof Error ? e.message : 'The run could not be loaded.'); }
  };

  const res = open?.result as Cfg | null | undefined;
  const sched = res?.fee_schedule as Cfg | undefined;
  const scheduleOk = Boolean(res) && !sched?.unavailable;
  const trades = useMemo(() => ((res?.trades ?? []) as Cfg[]), [res]);
  const runSegment = String(open?.request?.segment ?? segment);

  return (
    <div className="sl-stack">
      <Notice tone="warn"><strong>Research only · decision support, not investment advice.</strong> Every result here is a hypothetical backtest on stored candles. It is never a paper position, an order or real P&amp;L, and nothing on this page can place an order.</Notice>
      {error && <Notice tone="bad">{error}</Notice>}
      {notice && <Notice tone="ok">{notice}</Notice>}
      <nav className="sl-tabs" aria-label="Intraday research sections">
        {SUBTABS.map(([k, l]) => <button key={k} type="button" className={sub === k ? 'active' : ''} aria-current={sub === k ? 'page' : undefined} onClick={() => setSub(k)}>{l}</button>)}
      </nav>
      {open && sub !== 'overview' && sub !== 'config' && <p className="sl-muted">Showing run {open.id.slice(0, 8)} · <StatusPill status={open.status} /> · {(open.request.underlyings as string[] | undefined)?.join(', ')} · {show(open.request.segment)}</p>}

      {sub === 'overview' && (
        <section className="sl-card" aria-label="Overview">
          <h2>VAR — Value Acceptance &amp; Rejection <span className="sl-muted">v{show(cfg?.version)}</span></h2>
          <div className="sl-grid2">
            <div><h3>Strategy</h3><ul>
              <li>Variants: <strong>A</strong> volume profile + VWAP (runs on stored candles); <strong>B–D</strong> add delta/CVD, TPO, absorption (need order flow, which is not stored).</li>
              <li>Engines: acceptance breakout; failed-auction reversal (independent, reported separately).</li>
              <li>Result type: <Pill tone="warn">HYPOTHETICAL backtest</Pill> — not paper, not live.</li>
              <li>Config version: {version || 'default (unsaved)'} · scalp_hold runs under the Backtesting tab of this workspace.</li></ul></div>
            <div><h3>This run</h3>{open ? (
              <ul><li>Status: <StatusPill status={open.status} /></li><li>Segment: {show(open.request.segment)}</li><li>Instruments: {(open.request.underlyings as string[] | undefined)?.join(', ') ?? '—'}</li>
                <li>Date range: {show(res?.window?.start ?? open.request.start)} → {show(res?.window?.end ?? open.request.end)} ({show(res?.window?.sessions)} sessions)</li>
                <li>Data level: {(res?.window?.data_levels ?? []).join(', ') || '—'} · provider: stored Dhan intraday candles</li><li>Fee schedule: {show(sched?.schedule_version ?? res?.assumptions?.fee_schedule_version)}</li></ul>
            ) : <p className="sl-muted">No run selected. Pick one from Config &amp; run.</p>}</div>
          </div>
          <h3>Data quality and limitations</h3>
          {!caps ? <p className="sl-muted">Loading data capability…</p> : (
            <>
              <p><Pill tone="warn">Data level: candles (approximate)</Pill> <Pill tone="bad">Order flow: unavailable</Pill></p>
              <Scroll label="Stored futures candles"><table className="sl-table"><thead><tr><th>Underlying</th><th>Type</th><th>Contracts</th><th>1m bars</th><th>First</th><th>Last</th><th>Sessions</th></tr></thead>
                <tbody>{caps.futures_candles_1m.length === 0 ? <tr><td colSpan={7}>No futures candles stored</td></tr> : caps.futures_candles_1m.map((r) => <tr key={r.underlying}><td>{r.underlying}</td><td>{r.type}</td><td>{r.contracts}</td><td>{r.bars}</td><td>{show(r.first)}</td><td>{show(r.last)}</td><td>{r.sessions}</td></tr>)}</tbody></table></Scroll>
              <div className="sl-grid2"><div><h4>Supported</h4><ul>{caps.supports.map((s) => <li key={s}>{s}</li>)}</ul></div><div><h4>Not supported by stored data</h4><ul>{caps.does_not_support.map((s) => <li key={s}>{s}</li>)}</ul></div></div>
              <ul className="sl-muted"><li>{caps.order_flow}</li><li>Volume unit: {caps.volume_unit}. Tick size: {caps.tick_size_unit}. {caps.lot_size_note}. {caps.session_note}.</li>
                <li>Stored futures minute volume contains many zero-volume minutes in liquid contracts; treat volume-driven conditions as approximate.</li>
                <li>Equity-cash 1-minute history is truncated at 15:14 on most sessions; only symbols with full sessions can be backtested.</li>
                <li>Sample-size rule: no performance claim is made below 100 trades.</li></ul>
            </>
          )}
        </section>
      )}

      {sub === 'config' && (
        <>
          <section className="sl-card" aria-label="Strategy settings">
            <h2>Settings <span className="sl-muted">(saved version {version || 'default'})</span></h2>
            {!cfg ? <p className="sl-muted">Loading settings…</p> : (
              <>
                <div className="sl-grid2">
                  <label>Variant<select value={cfg.variant} onChange={(e) => setCfg({ ...cfg, variant: e.target.value })}>
                    <option value="A">A · Volume Profile + VWAP (runs on candles)</option><option value="B">B · + Delta/CVD (needs order flow)</option>
                    <option value="C">C · + TPO (needs order flow)</option><option value="D">D · + absorption/divergence (needs order flow)</option></select></label>
                  <label>Exit model<select value={cfg.exits.exit_model} onChange={(e) => setCfg({ ...cfg, exits: { ...cfg.exits, exit_model: e.target.value } })}>
                    <option value="fixed_partial">Fixed partial + target</option><option value="structural_trail">Structural trail</option></select></label>
                  <fieldset><legend>Engines</legend>
                    {['breakout', 'reversal'].map((n) => <label key={n}><input type="checkbox" checked={(cfg.engines ?? ['breakout', 'reversal']).includes(n)} onChange={() => toggleEngine(n)} /> {n}</label>)}</fieldset>
                  {SETTINGS.map((s) => <label key={s.path.join('.')}>{s.label}<input type="number" step={s.step} value={cfg[s.path[0]][s.path[1]]} onChange={(e) => setField(s.path[0], s.path[1], e.target.value)} /></label>)}
                </div>
                <p className="sl-muted">k is defined as (G − C) ÷ C ≥ k, where G is the gross target reward in rupees and C the estimated round-trip charges, i.e. G ≥ (1 + k)·C. It is not the gross R:R. Lowering it admits trades whose reward barely clears charges.</p>
                {cfg.variant !== 'A' && <Notice tone="warn">Variants B–D require order flow, which is not stored. They will produce no trades; every candidate is rejected with “order flow unavailable”.</Notice>}
                <button type="button" onClick={() => void save()}>Save as new version</button>
              </>
            )}
          </section>
          <section className="sl-card" aria-label="Launch backtest">
            <h2>Backtest on stored candles</h2>
            <div className="sl-grid2">
              <label>Segment<select value={segment} onChange={(e) => { setSegment(e.target.value); setUnders(e.target.value === 'index_futures' ? 'NIFTY, BANKNIFTY' : e.target.value === 'equity_cash' ? 'SBIN' : 'RELIANCE, HDFCBANK, TCS'); }}>
                <option value="index_futures">NSE index futures</option><option value="stock_futures">NSE stock futures</option><option value="equity_cash">NSE equity cash (intraday / MIS)</option></select></label>
              <label>Instruments (comma separated)<input value={unders} onChange={(e) => setUnders(e.target.value)} /></label>
              <label>Initial capital (₹)<input value={capital} onChange={(e) => setCapital(e.target.value)} inputMode="decimal" /></label>
              <label>Start<input type="date" value={start} onChange={(e) => setStart(e.target.value)} /></label>
              <label>End<input type="date" value={end} onChange={(e) => setEnd(e.target.value)} /></label>
              {segment === 'stock_futures' && <label>Point-in-time top-N liquid universe (optional)<input type="number" min={1} max={50} value={topN} onChange={(e) => setTopN(e.target.value)} /></label>}
              <label>Broker profile<select value="dhan" disabled><option value="dhan">Dhan</option></select></label>
              <label>Exchange<select value="NSE" disabled><option value="NSE">NSE</option></select></label>
              <label>Fee schedule version<input value="current (recorded on every result)" disabled /></label>
            </div>
            <button type="button" onClick={() => void check()}>Check data first</button>
            {pre && (
              <Scroll label="Data check"><table className="sl-table"><thead><tr><th>Symbol</th><th>Status</th><th>Sessions stored</th><th>Usable sessions</th><th>Detail</th></tr></thead>
                <tbody>{pre.map((p) => (
                  <tr key={p.symbol}><th scope="row">{p.symbol}</th><td><StatusPill status={p.status === 'ok' ? 'ok' : 'no_data'} /> {String(p.status).replace(/_/g, ' ')}</td><td>{show(p.sessions_available)}</td><td>{show(p.sessions_usable)}</td>
                    <td>{show(p.reason || (p.warnings as string[] | undefined)?.[0])}</td></tr>))}</tbody></table></Scroll>)}
            {pre && usable === 0 && <Notice tone="bad">None of these symbols has usable stored data for this segment and window. A run would replay nothing, so launching is disabled.</Notice>}
            {pre && usable > 0 && unusable > 0 && <label><input type="checkbox" checked={allowPartial} onChange={(e) => setAllowPartial(e.target.checked)} /> Run only the {usable} usable symbol(s); skip the {unusable} without usable data</label>}
            <button type="button" disabled={Boolean(pre) && (usable === 0 || (unusable > 0 && !allowPartial))} onClick={() => void launch()}>Run backtest</button>
            {runs.length === 0 ? <Empty title="No backtests yet">Run one above. Nothing is shown until real stored candles have been replayed.</Empty> : (
              <Scroll label="Backtest runs"><table className="sl-table"><thead><tr><th>Created</th><th>Status</th><th>Segment</th><th>Instruments</th><th>Trades</th><th /></tr></thead>
                <tbody>{runs.map((r) => (
                  <tr key={r.id}><td>{r.created_at.slice(0, 16).replace('T', ' ')}</td><td><StatusPill status={r.status} /></td><td>{show(r.request.segment)}</td>
                    <td>{(r.request.underlyings as string[] | undefined)?.join(', ') ?? '—'}</td><td>{show((r.result as Cfg | null)?.summary?.trades)}</td>
                    <td><button type="button" onClick={() => void view(r.id, 'performance')}>Open</button></td></tr>))}</tbody></table></Scroll>)}
          </section>
        </>
      )}

      {sub === 'charges' && <VarCharges segment={runSegment} asOf={res?.window?.end as string | undefined} runSchedule={sched} />}

      {sub !== 'overview' && sub !== 'config' && sub !== 'charges' && (!open ? <Empty title="No run selected">Open a run from Config &amp; run.</Empty> : open.error ? <Notice tone="bad">{open.error}</Notice> : !res ? <p className="sl-muted">The run has not finished.</p> : (
        <>
          <VarDataAudit audit={res.data_audit as Cfg[] | undefined} status={String(res.status ?? open.status)} reason={res.reason as string | undefined} />
          {open.status === 'completed' && res.summary ? (
            <>
              {sub === 'funnel' && <VarFunnel result={res} />}
              {sub === 'performance' && <VarPerformance result={res} scheduleOk={scheduleOk} />}
              {sub === 'ledger' && <VarLedger trades={trades} scheduleOk={scheduleOk} />}
              {sub === 'performance' && (res.notes as string[] | undefined)?.length ? <details><summary>Data notes ({(res.notes as string[]).length})</summary><ul>{(res.notes as string[]).slice(0, 20).map((n) => <li key={n}>{n}</li>)}</ul></details> : null}
              <p className="sl-muted">Window {show(res.window?.start)} → {show(res.window?.end)} · {show(res.window?.bars)} 5m bars · {money(open.request.initial_capital)} starting capital · {show(res.assumptions?.candle_fill_model)} fill model · stop assumed first when stop and target share a bar.</p>
            </>
          ) : (open.status === 'queued' || open.status === 'running') ? <p className="sl-muted">The run has not finished.</p>
            : <Empty title={`Not completed: ${open.status.replace(/_/g, ' ')}`}>No funnel, performance or ledger is shown because the run did not replay any data.</Empty>}
        </>
      ))}
    </div>
  );
}
