import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { apiRequest, downloadCsv } from '../api';
import { fmtMoney, fmtPct, lab, messageOf } from './lab';
import { Field, Notice, Pill, Scroll } from './shared';
import { LineChart } from './charts';
import { TradePlot } from './TradePlot';
import type { BackfillJob, Meta, StrategyRow, Trade } from './types';

type Coverage = { needed_bars: number; needed_sessions: number; symbols_checked: number; qualifying: number; best_bars: number; best_symbol: string | null; best_sessions: number; median_bars: number };
type Preset = { key: string; label: string; available: number; symbols: string[]; excluded: { symbol: string; reason: string; instrument_id?: string }[]; min_bars_1m: number; coverage?: Coverage | null };
type Universes = { presets: Preset[]; max_strategies: number; max_instruments: number; note: string };
type Headline = { total_trades?: number | null; net_profit?: string | null; profit_factor_net?: number | string | null; win_rate?: number | null; max_drawdown_pct?: number | null; expectancy_per_trade?: string | null; return_pct?: number | null };
type Metrics = Headline & { gross_pnl_before_costs?: string | null; total_charges?: string | null; total_slippage?: string | null; sharpe_ratio?: number | null };
export type LabRow = {
  run_id: string; strategy_id: string; strategy: string; kind: string; status: string; error?: string | null; progress?: number; rank: number | null; low_sample: boolean;
  metrics?: Metrics; in_sample?: Headline | null; out_of_sample?: Headline | null; split_at?: string | null;
  by_instrument?: Record<string, { trades: number | null; net_pnl: string | null; win_rate: number | null; expectancy: string | null }> | null;
  symbols_profitable?: number; symbols_tested?: number; months_profitable?: number; months_tested?: number; cost_profile?: string | null;
  refused_instruments?: { symbol: string; reason: string }[];
  walk_forward?: { folds: number; folds_profitable: number; train_days: number; test_days: number; parameters: string[]; optimised_on: string; fold_results: { test: string[] | null; chosen_params: Record<string, number> | null; net_profit: string | null; trades: number | null }[] };
  sweep?: { parameters: string[]; points: number; points_profitable: number; best: string | null; worst: string | null; median: string | null; window: string | null; note: string | null; grid: Record<string, unknown>[] };
};
type Comparison = {
  columns: { run_id: string; strategy: string; metrics: Record<string, string | number | null> | null; monthly: Record<string, string | null>; by_instrument: Record<string, string | null> }[];
  shared_symbols: string[]; months: string[]; metric_keys: string[]; best: Record<string, string | null>; note: string;
};
type RunChartData = { trades?: Trade[]; equity_curve?: { t: string; equity: string | number; drawdown_pct?: number }[] };

/** One click to fetch the missing 1-minute history for the symbols a preset had to leave out (existing batch backfill; market data only). */
function FetchHistory({ ids, onDone }: { ids: string[]; onDone: () => void }) {
  const [job, setJob] = useState<BackfillJob | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const running = !!job && job.status !== 'completed' && job.status !== 'failed';
  useEffect(() => {
    if (!job || !running) return undefined;
    const timer = window.setInterval(() => {
      apiRequest<BackfillJob>(`/market-data/history/sync-jobs/${job.id}`).then((next) => { setJob(next); if (next.status === 'completed' || next.status === 'failed') onDone(); }).catch((e) => setError(messageOf(e)));
    }, POLL_MS);
    return () => window.clearInterval(timer);
  }, [job, running, onDone]);
  const start = async () => {
    setStarting(true); setError(null);
    try { setJob(await apiRequest<BackfillJob>('/market-data/backfill/intraday-1m', { method: 'POST', body: JSON.stringify({ instrument_ids: ids.slice(0, FETCH_MAX), start: isoDate(FETCH_DAYS), end: isoDate(0) }) })); }
    catch (e) { setError(messageOf(e)); } finally { setStarting(false); }
  };
  const count = Math.min(ids.length, FETCH_MAX);
  const done = job?.instruments.filter((i) => !['queued', 'running', 'waiting_rate_limit'].includes(i.status)).length ?? 0;
  const failed = job?.instruments.filter((i) => i.status === 'failed').length ?? 0;
  return (
    <div className="sl-toolbar">
      <button type="button" className="sl-primary" disabled={starting || running} onClick={() => void start()}>{starting || running ? 'Fetching history…' : `Fetch 1-minute history for ${count} symbols`}</button>
      {job && <span className="sl-muted" role="status">{job.status === 'failed' ? 'The fetch stopped with an error. Try again.' : `${done} of ${job.instruments.length} done${failed ? `, ${failed} failed` : ''}${running ? '. This takes several minutes; you can leave this open.' : '.'}`}</span>}
      {error && <span className="sl-muted" role="alert">{error}</span>}
    </div>
  );
}

/** Loads one finished lab run on demand and plots its trades on candles plus its hypothetical equity curve. */
function RunCharts({ runId, symbol }: { runId: string; symbol?: string | null }) {
  const [data, setData] = useState<RunChartData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const load = async () => {
    setLoading(true); setError(null);
    try { setData((await lab<{ result?: RunChartData }>(`/backtests/${runId}`)).result ?? {}); } catch (e) { setError(messageOf(e)); } finally { setLoading(false); }
  };
  useEffect(() => { if (symbol) void load(); }, [runId, symbol]); // eslint-disable-line react-hooks/exhaustive-deps
  if (!data) return <div className="lab-run-charts"><button type="button" onClick={() => void load()} disabled={loading}>{loading ? 'Loading chart…' : 'Show trades on chart'}</button>{error && <p className="sl-muted" role="alert">{error}</p>}</div>;
  const trades = (data.trades ?? []).filter((t) => !symbol || t.symbol === symbol);
  const curve = data.equity_curve ?? [];
  return (
    <div className="lab-run-charts">
      {trades.length > 0 ? <TradePlot key={symbol ?? 'all'} trades={trades} /> : <p className="sl-muted">{symbol ? `No stored trades for ${symbol} in this run.` : 'This run stored no individual trades, so there is nothing to plot on candles.'}</p>}
      {curve.length > 1 && <><h4>Equity curve (hypothetical)</h4><LineChart points={curve.map((p) => ({ t: p.t, v: Number(p.equity) }))} label="Equity (hypothetical)" fill /></>}
      <p className="sl-muted">Hypothetical backtest result, never real P&amp;L, an order or a position.</p>
    </div>
  );
}

type Kind = 'single' | 'is_oos' | 'walk_forward' | 'sensitivity';
const KIND_NAME: Record<Kind, string> = { single: 'Single run (whole window)', is_oos: 'In-sample vs out-of-sample', walk_forward: 'Walk-forward (rolling train → test)', sensitivity: 'Parameter sweep (robustness)' };
const METRIC_LABEL: Record<string, string> = { total_trades: 'Trades', win_rate: 'Win rate', gross_pnl_before_costs: 'Gross before costs', total_charges: 'Charges', total_slippage: 'Slippage', net_profit: 'Net profit', profit_factor_net: 'Profit factor (net)', expectancy_per_trade: 'Expectancy / trade', max_drawdown_pct: 'Max drawdown %', sharpe_ratio: 'Sharpe' };
type Batch = { batch_id: string; name: string | null; kind: string; created_at: string; finished: boolean; progress: { done: number; total: number }; window: { start?: string; end?: string }; rows: LabRow[]; label: string; min_trades: number };
type BatchListItem = { batch_id: string; name: string | null; kind: string; created_at: string; runs: number; completed: number; failed: number };
type StartResult = { batch_id: string; runs: unknown[]; rejected: { strategy_id: string; reason: unknown }[]; excluded: { symbol: string; reason: string }[]; instruments: { symbol: string }[] };

type SortKey = 'rank' | 'strategy' | 'trades' | 'win' | 'net' | 'pf' | 'exp' | 'dd' | 'breadth';
const POLL_MS = 2500;
const FETCH_DAYS = 100;      // calendar days: comfortably more than the ~53 sessions a backtest needs
const FETCH_MAX = 50;        // the backfill endpoint's limit per request
const num = (v: unknown): number | null => { if (v === null || v === undefined || v === '') return null; const n = Number(v); return Number.isFinite(n) ? n : null; };
const tone = (v: unknown) => { const n = num(v); return n === null || n === 0 ? '' : n > 0 ? 'sl-pos' : 'sl-neg'; };
const isoDate = (daysAgo: number) => new Date(Date.now() - daysAgo * 86_400_000).toISOString().slice(0, 10);

function sortValue(row: LabRow, key: SortKey): number | string | null {
  const m = row.metrics ?? {};
  switch (key) {
    case 'rank': return row.rank;
    case 'strategy': return row.strategy;
    case 'trades': return num(m.total_trades);
    case 'win': return num(m.win_rate);
    case 'net': return num(m.net_profit);
    case 'pf': return num(m.profit_factor_net);
    case 'exp': return num(m.expectancy_per_trade);
    case 'dd': return num(m.max_drawdown_pct);
    case 'breadth': return row.symbols_tested ? (row.symbols_profitable ?? 0) / row.symbols_tested : null;
  }
}

export function BacktestLab({ meta, strategies }: { meta: Meta; strategies: StrategyRow[] }) {
  const intraday = useMemo(() => strategies.filter((s) => s.kind === 'intraday' && s.status !== 'retired'), [strategies]);
  const [universes, setUniverses] = useState<Universes | null>(null);
  const [picked, setPicked] = useState<string[]>([]);
  const [preset, setPreset] = useState('nifty50');
  const [start, setStart] = useState(isoDate(365));
  const [end, setEnd] = useState(isoDate(0));
  const [costProfile, setCostProfile] = useState('');
  const [slippage, setSlippage] = useState('2');
  const [kind, setKind] = useState<Kind>('single');
  const [trainDays, setTrainDays] = useState('20');
  const [testDays, setTestDays] = useState('5');
  const [steps, setSteps] = useState('1');
  const [chosen, setChosen] = useState<string[]>([]);
  const [comparison, setComparison] = useState<Comparison | null>(null);
  const [split, setSplit] = useState('0.7');
  const [name, setName] = useState('');
  const [minTrades, setMinTrades] = useState(30);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [batch, setBatch] = useState<Batch | null>(null);
  const [history, setHistory] = useState<BatchListItem[]>([]);
  const [sort, setSort] = useState<{ key: SortKey; dir: 1 | -1 }>({ key: 'rank', dir: 1 });
  const [open, setOpen] = useState<string | null>(null);
  const [plotSym, setPlotSym] = useState<{ runId: string; symbol: string } | null>(null);
  const activeId = useRef<string | null>(null);

  const loadHistory = useCallback(() => lab<{ batches: BatchListItem[] }>('/lab/batches').then((r) => setHistory(r.batches)).catch(() => undefined), []);
  const loadUniverses = useCallback(() => { void lab<Universes>('/lab/universes').then(setUniverses).catch((e) => setError(messageOf(e))); }, []);
  useEffect(() => { loadUniverses(); void loadHistory(); }, [loadUniverses, loadHistory]);

  const loadBatch = useCallback(async (id: string, min: number) => {
    const b = await lab<Batch>(`/lab/batches/${id}?min_trades=${min}`);
    if (activeId.current === id) setBatch(b);
    return b;
  }, []);
  // poll while the batch is still running; stop as soon as every run has finished
  useEffect(() => {
    if (!batch || batch.finished) return undefined;
    const id = batch.batch_id;
    const timer = window.setInterval(() => { void loadBatch(id, minTrades).then((b) => { if (b.finished) void loadHistory(); }).catch((e) => setError(messageOf(e))); }, POLL_MS);
    return () => window.clearInterval(timer);
  }, [batch, minTrades, loadBatch, loadHistory]);

  const current = universes?.presets.find((p) => p.key === preset);
  const toggle = (id: string) => setPicked((cur) => (cur.includes(id) ? cur.filter((x) => x !== id) : cur.length >= (universes?.max_strategies ?? 12) ? cur : [...cur, id]));

  async function run() {
    setBusy(true); setError(null); setNotice(null);
    try {
      const config: Record<string, unknown> = { start: `${start}T00:00:00Z`, end: `${end}T23:59:59Z`, slippage_bps: slippage };
      if (costProfile) config.cost_profile = costProfile;
      const r = await lab<StartResult>('/lab/batches', { method: 'POST', body: { name, strategy_ids: picked, universe: preset, kind, split_ratio: Number(split), train_days: Number(trainDays), test_days: Number(testDays), steps: Number(steps), config } });
      activeId.current = r.batch_id;
      setOpen(null); setChosen([]); setComparison(null);
      const parts = [`Queued ${r.runs.length} strategy run(s) on ${r.instruments.length} symbols.`];
      if (r.excluded.length) parts.push(`${r.excluded.length} symbol(s) left out for lack of stored 1-minute data.`);
      if (r.rejected.length) parts.push(`${r.rejected.length} strategy(ies) could not be queued.`);
      setNotice(parts.join(' '));
      await loadBatch(r.batch_id, minTrades);
      void loadHistory();
    } catch (e) { setError(messageOf(e)); } finally { setBusy(false); }
  }

  async function openBatch(id: string) {
    setError(null); activeId.current = id;
    try { await loadBatch(id, minTrades); } catch (e) { setError(messageOf(e)); }
  }

  const rows = useMemo(() => {
    if (!batch) return [];
    const { key, dir } = sort;
    return [...batch.rows].sort((a, b) => {
      const x = sortValue(a, key), y = sortValue(b, key);
      if (x === null || y === null) return x === y ? 0 : x === null ? 1 : -1;   // unknown always last
      return (typeof x === 'string' ? x.localeCompare(String(y)) : (x as number) - (y as number)) * dir;
    });
  }, [batch, sort]);
  const sortBy = (key: SortKey) => setSort((s) => (s.key === key ? { key, dir: (s.dir * -1) as 1 | -1 } : { key, dir: key === 'strategy' || key === 'rank' ? 1 : -1 }));
  const arrow = (key: SortKey) => (sort.key === key ? (sort.dir === 1 ? ' ▲' : ' ▼') : '');
  const oos = batch?.kind === 'is_oos';
  const wf = batch?.kind === 'walk_forward';
  const sweep = batch?.kind === 'sensitivity';
  const single = batch?.kind === 'single';
  const toggleChosen = (id: string) => setChosen((cur) => (cur.includes(id) ? cur.filter((x) => x !== id) : cur.length >= 6 ? cur : [...cur, id]));
  async function compare() {
    setError(null);
    try { setComparison(await lab<Comparison>(`/lab/compare?runs=${chosen.join(',')}`)); } catch (e) { setError(messageOf(e)); }
  }

  return (
    <div className="sl-stack">
      <Notice tone="info">Hypothetical backtests on stored real 1-minute bars (intraday timeframes are built from them), after charges and slippage. They are not real P&amp;L, a position, or an order.</Notice>
      {error && <Notice tone="bad">{error}</Notice>}
      {notice && <Notice tone="ok">{notice}</Notice>}

      <div className="sl-grid2">
        <section className="sl-card" aria-label="Lab setup">
          <div className="sl-card-head"><h3>1 · Strategies</h3><Pill tone="neutral">{picked.length}/{universes?.max_strategies ?? 12}</Pill></div>
          {intraday.length === 0 ? <p className="sl-muted">No intraday strategies yet. Install the built-in or registry strategies from the Library, or build one.</p> : (
            <ul className="lab-picklist" aria-label="Strategies to compare">
              {intraday.map((s) => (
                <li key={s.id}><label><input type="checkbox" checked={picked.includes(s.id)} onChange={() => toggle(s.id)} /> <strong>{s.name}</strong>
                  <small> {s.decision_timeframe}{s.origin_label ? ` · ${s.origin_label}` : ''}</small></label></li>
              ))}
            </ul>
          )}
          <div className="sl-card-head"><h3>2 · Symbols</h3></div>
          <Field label="Symbol set">
            <select value={preset} onChange={(e) => setPreset(e.target.value)} aria-label="Symbol set">
              {universes?.presets.map((p) => <option key={p.key} value={p.key}>{p.label} — {p.available} usable</option>)}
            </select>
          </Field>
          {current && (
            <>
            {current.available === 0 && current.coverage && current.coverage.symbols_checked > 0 && <p className="sl-muted" role="status">No symbol qualifies yet: a backtest needs {current.coverage.needed_bars.toLocaleString('en-IN')} stored 1-minute bars (about {current.coverage.needed_sessions} sessions). Of {current.coverage.symbols_checked} checked, the best is {current.coverage.best_symbol} with {current.coverage.best_bars.toLocaleString('en-IN')} bars (about {current.coverage.best_sessions} sessions) and the median has {current.coverage.median_bars.toLocaleString('en-IN')}. Fetch the missing history below.</p>}
            {current.excluded.some((e) => e.instrument_id) && <FetchHistory key={current.key} ids={current.excluded.flatMap((e) => (e.instrument_id ? [e.instrument_id] : []))} onDone={loadUniverses} />}
            <details className="lab-coverage"><summary>{current.available} symbols with enough 1-minute data{current.excluded.length ? ` · ${current.excluded.length} left out` : ''}</summary>
              <p className="sl-muted">Using: {current.symbols.join(', ') || '—'}</p>
              {current.excluded.length > 0 && <ul>{current.excluded.slice(0, 60).map((e) => <li key={e.symbol}><strong>{e.symbol}</strong> — {e.reason}</li>)}</ul>}
            </details>
            </>
          )}
          <div className="sl-card-head"><h3>3 · Window and costs</h3></div>
          <div className="sl-row">
            <Field label="From"><input type="date" value={start} onChange={(e) => setStart(e.target.value)} /></Field>
            <Field label="To"><input type="date" value={end} onChange={(e) => setEnd(e.target.value)} /></Field>
            <Field label="Slippage (bps per side)"><input type="number" min={0} max={500} step="0.5" value={slippage} onChange={(e) => setSlippage(e.target.value)} /></Field>
          </div>
          <Field label="Charges">
            <select value={costProfile} onChange={(e) => setCostProfile(e.target.value)} aria-label="Charge schedule">
              <option value="">Default for the market (Dhan · NSE · intraday)</option>
              {meta.cost_profiles.filter((c) => c.market === 'equity_in').map((c) => <option key={c.id} value={c.id}>{c.label}</option>)}
            </select>
          </Field>
          <div className="sl-row">
            <Field label="Mode">
              <select value={kind} onChange={(e) => setKind(e.target.value as Kind)} aria-label="Mode">
                {(Object.keys(KIND_NAME) as Kind[]).map((k) => <option key={k} value={k}>{KIND_NAME[k]}</option>)}
              </select>
            </Field>
            {kind === 'walk_forward' && <><Field label="Train days"><input type="number" min={5} max={250} value={trainDays} onChange={(e) => setTrainDays(e.target.value)} /></Field>
              <Field label="Test days"><input type="number" min={1} max={60} value={testDays} onChange={(e) => setTestDays(e.target.value)} /></Field></>}
            {kind === 'sensitivity' && <Field label="Grid steps (±25% each)"><select value={steps} onChange={(e) => setSteps(e.target.value)} aria-label="Grid steps"><option value="1">1 (3 values per parameter)</option><option value="2">2 (5 values per parameter)</option></select></Field>}
            {kind === 'is_oos' && <Field label="In-sample share"><input type="number" min={0.3} max={0.85} step="0.05" value={split} onChange={(e) => setSplit(e.target.value)} /></Field>}
            <Field label="Batch name (optional)"><input value={name} maxLength={80} onChange={(e) => setName(e.target.value)} /></Field>
          </div>
          <div className="sl-toolbar"><button type="button" className="sl-primary" disabled={busy || picked.length === 0 || !current?.available} onClick={() => void run()}>
            {busy ? 'Queuing…' : `Run ${picked.length || ''} ${picked.length === 1 ? 'strategy' : 'strategies'} on ${current?.available ?? 0} symbols`}</button></div>
        </section>

        <section className="sl-card" aria-label="Recent batches">
          <div className="sl-card-head"><h3>Recent batches</h3></div>
          {history.length === 0 ? <p className="sl-muted">No batches yet.</p> : (
            <ul className="lab-history">
              {history.map((b) => (
                <li key={b.batch_id}><button type="button" className={b.batch_id === batch?.batch_id ? 'active' : ''} onClick={() => void openBatch(b.batch_id)}>
                  <strong>{b.name || `Batch ${b.batch_id.slice(0, 6)}`}</strong> <small>{b.kind === 'is_oos' ? 'IS/OOS' : 'single'} · {b.completed}/{b.runs} done{b.failed ? ` · ${b.failed} failed` : ''}</small></button></li>
              ))}
            </ul>
          )}
        </section>
      </div>

      {batch && (
        <section className="sl-card" aria-label="Batch results">
          <div className="sl-card-head">
            <h3>{batch.name || `Batch ${batch.batch_id.slice(0, 6)}`} {batch.finished ? <Pill tone="ok">finished</Pill> : <Pill tone="info">running {batch.progress.done}/{batch.progress.total}</Pill>}</h3>
            <div className="sl-row">
              <Field label="Min trades to rank"><input type="number" min={1} value={minTrades} onChange={(e) => { const v = Math.max(1, Number(e.target.value) || 1); setMinTrades(v); void loadBatch(batch.batch_id, v); }} /></Field>
              <button type="button" onClick={() => void downloadCsv(`/signals-lab/lab/batches/${batch.batch_id}/export.csv`, `lab-batch-${batch.batch_id.slice(0, 8)}.csv`)}>Export CSV</button>
            </div>
          </div>
          <p className="sl-muted">{batch.label} Window {batch.window.start?.slice(0, 10) ?? '—'} → {batch.window.end?.slice(0, 10) ?? '—'}. Ranked by net profit factor after charges; rows under {batch.min_trades} trades are unranked.</p>
          {single && chosen.length > 0 && (
            <div className="sl-toolbar" role="status">
              <span>{chosen.length} selected for comparison</span>
              <button type="button" className="sl-primary" disabled={chosen.length < 2} onClick={() => void compare()}>Compare {chosen.length} runs</button>
              <button type="button" onClick={() => { setChosen([]); setComparison(null); }}>Clear</button>
            </div>
          )}
          <Scroll label="Ranked results">
            <table className="sl-table lab-table">
              <thead><tr>
                {single && <th aria-label="Select for comparison" />}
                <th><button type="button" onClick={() => sortBy('rank')}>#{arrow('rank')}</button></th>
                <th><button type="button" onClick={() => sortBy('strategy')}>Strategy{arrow('strategy')}</button></th>
                <th className="r"><button type="button" onClick={() => sortBy('trades')}>{sweep ? 'Avg trades' : 'Trades'}{arrow('trades')}</button></th>
                {!wf && !sweep && <th className="r"><button type="button" onClick={() => sortBy('win')}>Win %{arrow('win')}</button></th>}
                {single && <><th className="r">Gross</th><th className="r">Charges</th><th className="r">Slippage</th></>}
                <th className="r"><button type="button" onClick={() => sortBy('net')}>{oos ? 'OOS net' : wf ? 'Test-window net' : sweep ? 'Median net' : 'Net'}{arrow('net')}</button></th>
                {!wf && !sweep && <th className="r"><button type="button" onClick={() => sortBy('pf')}>{oos ? 'OOS PF' : 'PF'}{arrow('pf')}</button></th>}
                {oos && <th className="r">IS PF</th>}
                {wf && <th className="r">Folds +</th>}
                {sweep && <><th className="r">Worst</th><th className="r">Best</th><th className="r">Grid +</th></>}
                {!wf && !sweep && <th className="r"><button type="button" onClick={() => sortBy('exp')}>Expectancy{arrow('exp')}</button></th>}
                {!wf && !sweep && <th className="r"><button type="button" onClick={() => sortBy('dd')}>Max DD{arrow('dd')}</button></th>}
                {single && <th className="r"><button type="button" onClick={() => sortBy('breadth')}>Symbols +{arrow('breadth')}</button></th>}
              </tr></thead>
              <tbody>
                {rows.map((r) => {
                  const m = r.metrics ?? {};
                  const done = r.status === 'completed';
                  return (
                    <Fragment key={r.run_id}>
                      <tr className={open === r.run_id ? 'sl-selected' : ''} onClick={() => setOpen(open === r.run_id ? null : r.run_id)} aria-expanded={open === r.run_id}>
                        {single && <td onClick={(e) => e.stopPropagation()}>{done && <input type="checkbox" aria-label={`Compare ${r.strategy}`} checked={chosen.includes(r.run_id)} onChange={() => toggleChosen(r.run_id)} />}</td>}
                        <td>{r.rank ?? '—'}</td>
                        <td><strong>{r.strategy}</strong>{!done && <> <Pill tone={r.status === 'failed' ? 'bad' : 'info'} title={r.error ?? undefined}>{r.status}</Pill></>}{r.low_sample && <> <Pill tone="warn" title="Too few trades to rank">thin</Pill></>}</td>
                        <td className="r">{done ? m.total_trades ?? '—' : '—'}</td>
                        {!wf && !sweep && <td className="r">{done && m.win_rate != null ? fmtPct(Number(m.win_rate) * 100, 1) : '—'}</td>}
                        {single && <><td className="r">{done ? fmtMoney(m.gross_pnl_before_costs) : '—'}</td><td className="r">{done ? fmtMoney(m.total_charges) : '—'}</td><td className="r">{done ? fmtMoney(m.total_slippage) : '—'}</td></>}
                        <td className={`r ${tone(m.net_profit)}`}>{done ? fmtMoney(m.net_profit) : '—'}</td>
                        {!wf && !sweep && <td className={`r ${num(m.profit_factor_net) !== null ? (Number(m.profit_factor_net) >= 1 ? 'sl-pos' : 'sl-neg') : ''}`}>{done && m.profit_factor_net != null ? Number(m.profit_factor_net).toFixed(2) : '—'}</td>}
                        {oos && <td className="r">{r.in_sample?.profit_factor_net != null ? Number(r.in_sample.profit_factor_net).toFixed(2) : '—'}</td>}
                        {wf && <td className="r">{done && r.walk_forward ? `${r.walk_forward.folds_profitable}/${r.walk_forward.folds}` : '—'}</td>}
                        {sweep && <><td className={`r ${tone(r.sweep?.worst)}`}>{done ? fmtMoney(r.sweep?.worst) : '—'}</td><td className={`r ${tone(r.sweep?.best)}`}>{done ? fmtMoney(r.sweep?.best) : '—'}</td>
                          <td className="r">{done && r.sweep ? `${r.sweep.points_profitable}/${r.sweep.points}` : '—'}</td></>}
                        {!wf && !sweep && <td className={`r ${tone(m.expectancy_per_trade)}`}>{done ? fmtMoney(m.expectancy_per_trade) : '—'}</td>}
                        {!wf && !sweep && <td className="r">{done ? fmtPct(m.max_drawdown_pct) : '—'}</td>}
                        {single && <td className="r">{done && r.symbols_tested ? `${r.symbols_profitable}/${r.symbols_tested}` : '—'}</td>}
                      </tr>
                      {open === r.run_id && done && (
                        <tr className="lab-detail"><td colSpan={14}>
                          {r.run_id && <RunCharts key={`${r.run_id}-${plotSym?.runId === r.run_id ? plotSym.symbol : ''}`} runId={r.run_id} symbol={plotSym?.runId === r.run_id ? plotSym.symbol : null} />}
                          {r.walk_forward && (
                            <>
                              <p className="sl-muted">{r.walk_forward.folds} rolling folds: train {r.walk_forward.train_days} days → test {r.walk_forward.test_days} days. {r.walk_forward.optimised_on}. Parameters tuned: {r.walk_forward.parameters.join(', ') || 'none'}.</p>
                              <table className="sl-table" aria-label={`${r.strategy} folds`}>
                                <thead><tr><th>Test window</th><th>Chosen on the train window</th><th className="r">Trades</th><th className="r">Net</th></tr></thead>
                                <tbody>{r.walk_forward.fold_results.map((f, i) => (
                                  <tr key={i}><td>{f.test ? `${f.test[0].slice(0, 10)} → ${f.test[1].slice(0, 10)}` : '—'}</td><td>{f.chosen_params && Object.keys(f.chosen_params).length ? Object.entries(f.chosen_params).map(([k, v]) => `${k}=${v}`).join(', ') : 'defaults'}</td>
                                    <td className="r">{f.trades ?? '—'}</td><td className={`r ${tone(f.net_profit)}`}>{fmtMoney(f.net_profit)}</td></tr>))}</tbody>
                              </table>
                            </>
                          )}
                          {r.sweep && (
                            <>
                              <p className="sl-muted">{r.sweep.note} Parameters varied: {r.sweep.parameters.join(', ') || 'none'}. A strategy whose result collapses when a parameter moves is overfit.</p>
                              <Scroll label="Sweep grid">
                                <table className="sl-table" aria-label={`${r.strategy} sweep`}>
                                  <thead><tr><th>Parameters</th><th className="r">Trades</th><th className="r">Net</th><th className="r">PF</th></tr></thead>
                                  <tbody>{r.sweep.grid.map((g, i) => (
                                    <tr key={i}><td>{Object.entries((g.params as Record<string, number>) ?? {}).map(([k, v]) => `${k}=${v}`).join(', ')}</td><td className="r">{String(g.total_trades ?? '—')}</td>
                                      <td className={`r ${tone(g.net_profit)}`}>{fmtMoney(g.net_profit as string)}</td><td className="r">{g.profit_factor_net != null ? Number(g.profit_factor_net).toFixed(2) : '—'}</td></tr>))}</tbody>
                                </table>
                              </Scroll>
                            </>
                          )}
                          {r.by_instrument && (
                            <table className="sl-table" aria-label={`${r.strategy} by symbol`}>
                              <thead><tr><th>Symbol</th><th className="r">Trades</th><th className="r">Win %</th><th className="r">Net</th><th className="r">Expectancy</th></tr></thead>
                              <tbody>{Object.entries(r.by_instrument).sort((a, b) => (num(b[1].net_pnl) ?? -Infinity) - (num(a[1].net_pnl) ?? -Infinity)).map(([sym, v]) => (
                                <tr key={sym} className={plotSym?.runId === r.run_id && plotSym.symbol === sym ? 'active' : ''} style={{ cursor: 'pointer' }} title="Plot this stock's trades on candles" onClick={() => setPlotSym({ runId: r.run_id, symbol: sym })}><td><button type="button" className="rt-link" onClick={() => setPlotSym({ runId: r.run_id, symbol: sym })}>{sym}</button></td><td className="r">{v.trades ?? '—'}</td><td className="r">{v.win_rate != null ? fmtPct(v.win_rate * 100, 1) : '—'}</td>
                                  <td className={`r ${tone(v.net_pnl)}`}>{fmtMoney(v.net_pnl)}</td><td className={`r ${tone(v.expectancy)}`}>{fmtMoney(v.expectancy)}</td></tr>))}</tbody>
                            </table>
                          )}
                          {oos && <p className="sl-muted">In-sample net {fmtMoney(r.in_sample?.net_profit)} · out-of-sample net {fmtMoney(r.out_of_sample?.net_profit)} · split at {r.split_at?.slice(0, 10) ?? '—'}. Per-symbol detail is available in single-run mode.</p>}
                          {r.months_tested ? <p className="sl-muted">Profitable months: {r.months_profitable}/{r.months_tested}. Charge schedule: {r.cost_profile ?? '—'}.</p> : null}
                          {r.refused_instruments && r.refused_instruments.length > 0 && <p className="sl-muted">Refused for data quality: {r.refused_instruments.map((x) => `${x.symbol} (${x.reason})`).join('; ')}</p>}
                        </td></tr>
                      )}
                    </Fragment>
                  );
                })}
              </tbody>
            </table>
          </Scroll>
          {comparison && (
            <section aria-label="Run comparison" className="lab-compare">
              <div className="sl-card-head"><h3>Comparison</h3><button type="button" onClick={() => setComparison(null)}>Close</button></div>
              <p className="sl-muted">{comparison.note}</p>
              <Scroll label="Comparison table">
                <table className="sl-table">
                  <thead><tr><th>Metric</th>{comparison.columns.map((c) => <th key={c.run_id} className="r">{c.strategy}</th>)}</tr></thead>
                  <tbody>
                    {comparison.metric_keys.map((k) => (
                      <tr key={k}><td>{METRIC_LABEL[k] ?? k}</td>{comparison.columns.map((c) => {
                        const v = c.metrics?.[k];
                        const isBest = comparison.best[k] === c.run_id;
                        const text = v == null ? '—' : k === 'win_rate' ? fmtPct(Number(v) * 100, 1) : k === 'max_drawdown_pct' ? fmtPct(v) : ['total_trades', 'profit_factor_net', 'sharpe_ratio'].includes(k) ? String(Number.isInteger(Number(v)) ? v : Number(v).toFixed(2)) : fmtMoney(v);
                        return <td key={c.run_id} className={`r ${isBest ? 'lab-best' : ''}`} title={isBest ? 'Best in this row' : undefined}>{text}</td>;
                      })}</tr>
                    ))}
                    <tr className="lab-section"><td colSpan={comparison.columns.length + 1}>Net by month</td></tr>
                    {comparison.months.map((month) => <tr key={month}><td>{month}</td>{comparison.columns.map((c) => <td key={c.run_id} className={`r ${tone(c.monthly[month])}`}>{c.monthly[month] != null ? fmtMoney(c.monthly[month]) : '—'}</td>)}</tr>)}
                    <tr className="lab-section"><td colSpan={comparison.columns.length + 1}>Net by symbol (symbols in every run)</td></tr>
                    {comparison.shared_symbols.map((sym) => <tr key={sym}><td>{sym}</td>{comparison.columns.map((c) => <td key={c.run_id} className={`r ${tone(c.by_instrument[sym])}`}>{c.by_instrument[sym] != null ? fmtMoney(c.by_instrument[sym]) : '—'}</td>)}</tr>)}
                  </tbody>
                </table>
              </Scroll>
            </section>
          )}
        </section>
      )}
    </div>
  );
}
