import { Fragment, useEffect, useMemo, useRef, useState } from 'react';
import { BarChart, LineChart } from './charts';
import { DISCLAIMER, fmtMoney, fmtPct, fmtTime, lab, messageOf } from './lab';
import { OptionsCoveragePanel } from './OptionsPanel';
import { InstrumentPicker } from './InstrumentPicker';
import { preflight, type Quality } from './preflight';
import { Field, Notice, Pill, Scroll, Stat, StatusPill } from './shared';
import { EngineParamsPanel, type GeometryState } from './EngineParamsPanel';
import { ScalpHoldView, type ScalpHoldResult } from './ScalpHoldView';
import { StyledSelect } from './StyledSelect';
import type { Group2, InstrumentRow, Meta, OptionsReport, RunRow, SingleResult, StrategyRow, Trade } from './types';

type Kind = 'single' | 'is_oos' | 'walk_forward' | 'sensitivity';
const KIND_LABEL: Record<Kind, string> = { single: 'Single run', is_oos: 'In-sample vs out-of-sample', walk_forward: 'Walk-forward', sensitivity: 'Parameter sensitivity' };
const POLL_MS = 1200;

function isoDate(daysAgo: number): string {
  const d = new Date(Date.now() - daysAgo * 86_400_000);
  return d.toISOString().slice(0, 10);
}

export function Backtesting({ meta, strategies, instruments, initialStrategyId, onUsed, onStoredChanged }: {
  meta: Meta; strategies: StrategyRow[]; instruments: InstrumentRow[]; initialStrategyId: string | null; onUsed: () => void; onStoredChanged?: () => void;
}) {
  const intraday = useMemo(() => strategies.filter((s) => s.kind === 'intraday'), [strategies]);
  const [sid, setSid] = useState(initialStrategyId ?? intraday[0]?.id ?? '');
  const [detail, setDetail] = useState<StrategyRow | null>(null);
  const [version, setVersion] = useState<number | null>(null);
  const [chosen, setChosen] = useState<string[]>([]);
  const [start, setStart] = useState(isoDate(30));
  const [end, setEnd] = useState(isoDate(0));
  const [kind, setKind] = useState<Kind>('single');
  const [capital, setCapital] = useState('1000000');
  const [slip, setSlip] = useState('2');
  const [maxConc, setMaxConc] = useState('5');
  const [leverage, setLeverage] = useState('1');
  const [direction, setDirection] = useState('both');
  const [maxMissing, setMaxMissing] = useState('5');
  const [profile, setProfile] = useState('');
  const [overrides, setOverrides] = useState<Record<string, string>>({});
  const [options, setOptions] = useState({ train_days: '20', test_days: '5', split_ratio: '0.7', parameters: '' });
  const [run, setRun] = useState<RunRow | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [history, setHistory] = useState<RunRow[]>([]);
  const [compare, setCompare] = useState<string[]>([]);
  const [compareRows, setCompareRows] = useState<RunRow[]>([]);
  const [quality, setQuality] = useState<Record<string, unknown> | null>(null);
  const [quick, setQuick] = useState(false);
  const [engineParams, setEngineParams] = useState<Record<string, string>>({});
  const [geo, setGeo] = useState<GeometryState>({ status: 'idle', inputs: null });
  const [qmap, setQmap] = useState<Record<string, Quality | undefined>>({});
  const [view, setView] = useState<'summary' | 'equity' | 'monthly' | 'trades' | 'breakdown' | 'validation' | 'assumptions' | 'compare' | 'options'>('summary');
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => { if (initialStrategyId) setSid(initialStrategyId); }, [initialStrategyId]);
  useEffect(() => { if (!sid && intraday[0]) setSid(intraday[0].id); }, [intraday, sid]);
  useEffect(() => () => { if (timer.current) clearTimeout(timer.current); }, []);

  useEffect(() => {
    if (!sid) { setDetail(null); return; }
    let live = true;
    lab<StrategyRow>(`/strategies/${sid}`).then((s) => {
      if (!live) return;
      setDetail(s);
      setVersion(s.current_version);
      const u = s.versions?.find((v) => v.version === s.current_version)?.definition?.universe ?? [];
      setChosen(u);
      setProfile('');
    }).catch((e) => live && setError(messageOf(e)));
    lab<RunRow[]>(`/backtests?strategy_id=${sid}`).then((r) => live && setHistory(r)).catch(() => undefined);
    return () => { live = false; };
  }, [sid]);

  const def = detail?.versions?.find((v) => v.version === (version ?? detail.current_version))?.definition ?? null;
  const market = def?.market ?? 'equity_in';
  const isOptions = !!def?.options;
  const isEngine = (def?.source as { provenance?: { engine?: string } } | undefined)?.provenance?.engine === 'scalp_hold';
  useEffect(() => { if (isEngine && kind !== 'single' && kind !== 'walk_forward') setKind('single'); }, [isEngine, kind]);
  const profiles = meta.cost_profiles.filter((p) => p.market === market);
  const activeProfile = profiles.find((p) => p.id === (profile || profiles[0]?.id));
  const nameOf = (id: string) => instruments.find((i) => i.id === id)?.symbol ?? id.slice(0, 8);

  const windowDays = Math.max(1, Math.round((Date.parse(end) - Date.parse(start)) / 86_400_000));
  useEffect(() => {
    // Per-instrument data quality for the chosen window, loaded as soon as an instrument is picked so refusals are visible BEFORE running.
    let live = true;
    setQmap(Object.fromEntries(chosen.map((id) => [id, undefined as Quality | undefined])));
    chosen.forEach((id) => {
      lab<Quality>(`/data-quality/${id}?days=${windowDays}`)
        .then((q) => { if (live) setQmap((cur) => ({ ...cur, [id]: q })); })
        .catch(() => { if (live) setQmap((cur) => ({ ...cur, [id]: { status: 'refuse', bars: 0, missing_pct: null, issues: ['data quality could not be checked'] } })); });
    });
    return () => { live = false; };
  }, [chosen, windowDays, instruments]);
  const storedEngine = useMemo(() => ((def?.source as { provenance?: { params?: Record<string, unknown> } } | undefined)?.provenance?.params ?? {}), [def]);
  const geoBar = engineParams.bar_minutes?.trim() || String(storedEngine.bar_minutes ?? 5);
  const geoH = engineParams.H_minutes?.trim() || String(storedEngine.H_minutes ?? 30);
  useEffect(() => {
    // Live preview inputs: cost at the run's reference notional and the selected instrument's typical bar range for this window.
    if (!isEngine || !chosen.length) { setGeo({ status: 'idle', inputs: null }); return undefined; }
    let live = true;
    setGeo((cur) => ({ ...cur, status: 'loading' }));
    const q = new URLSearchParams({ instrument_id: chosen[0], start, end, bar_minutes: geoBar, H_minutes: geoH, equity: capital || '10000', leverage: leverage || '1' });
    const timer = window.setTimeout(() => {
      lab<{ available: boolean; symbol: string; reason?: string; cost_rt_pct?: number; sigma_H_median_pct?: number | null; typical_bar_pct?: number | null }>(`/engines/scalp_hold/geometry?${q}`)
        .then((g) => { if (!live) return; setGeo(g.available ? { status: 'ready', symbol: g.symbol, inputs: { cost_rt_pct: g.cost_rt_pct ?? null, sigma_H_median_pct: g.sigma_H_median_pct ?? null, typical_bar_pct: g.typical_bar_pct ?? null } } : { status: 'unavailable', symbol: g.symbol, reason: g.reason, inputs: null }); })
        .catch((e: unknown) => { if (live) setGeo({ status: 'unavailable', reason: messageOf(e), inputs: null }); });
    }, 400);
    return () => { live = false; window.clearTimeout(timer); };
  }, [isEngine, chosen, start, end, geoBar, geoH, capital, leverage]);
  const pre = preflight(chosen, qmap, (id) => instruments.find((i) => i.id === id)?.symbol ?? id.slice(0, 8), Number(maxMissing) || 0);

  const config = () => ({
    start: `${start}T00:00:00Z`, end: `${end}T23:59:59Z`, initial_capital: capital, slippage_bps: slip, max_concurrent: Number(maxConc), leverage, direction,
    max_missing_pct: Number(maxMissing), cost_profile: profile || null, cost_overrides: Object.fromEntries(Object.entries(overrides).filter(([, v]) => v !== '')),
  });

  const poll = (id: string) => {
    lab<RunRow>(`/backtests/${id}`).then((r) => {
      setRun(r);
      if (r.status === 'queued' || r.status === 'running') timer.current = setTimeout(() => poll(id), POLL_MS);
      else { setBusy(false); if (sid) lab<RunRow[]>(`/backtests?strategy_id=${sid}`).then(setHistory).catch(() => undefined); onUsed(); }
    }).catch((e) => { setBusy(false); setError(messageOf(e)); });
  };

  const start_ = async () => {
    if (!sid) return;
    setBusy(true); setError(null); setRun(null); setView(isOptions ? 'options' : 'summary');
    const opts: Record<string, unknown> = {};
    if (kind === 'walk_forward') { opts.train_days = Number(options.train_days); opts.test_days = Number(options.test_days); }
    if (kind === 'is_oos') opts.split_ratio = Number(options.split_ratio);
    if (isEngine && quick) opts.quick = true;
    if (isEngine) { const overrides = Object.fromEntries(Object.entries(engineParams).filter(([, v]) => v.trim() !== '')); if (Object.keys(overrides).length) opts.params = overrides; }
    if (options.parameters.trim()) opts.parameters = options.parameters.split(',').map((x) => x.trim()).filter(Boolean);
    try {
      const r = await lab<RunRow>('/backtests', { method: 'POST', body: { strategy_id: sid, version, kind, instruments: chosen, config: config(), options: opts } });
      setRun(r);
      poll(r.id);
    } catch (e) { setBusy(false); setError(messageOf(e)); }
  };

  const checkQuality = async () => {
    setQuality(null);
    try {
      const out: Record<string, unknown> = {};
      for (const id of chosen.slice(0, 8)) out[nameOf(id)] = await lab(`/data-quality/${id}?days=${Math.max(1, Math.round((Date.parse(end) - Date.parse(start)) / 86_400_000))}`);
      setQuality(out);
    } catch (e) { setError(messageOf(e)); }
  };

  const loadCompare = async (ids: string[]) => {
    setCompare(ids);
    setCompareRows(await Promise.all(ids.map((id) => lab<RunRow>(`/backtests/${id}`))));
  };

  const result = run?.status === 'completed' ? (run.result as Partial<SingleResult> & Record<string, unknown>) : null;
  const single = result && result.metrics ? (result as unknown as SingleResult) : null;

  if (!intraday.length) {
    return <Notice tone="info">Create or import an intraday strategy first. Legacy daily strategies are backtested in the “Legacy daily &amp; crypto research” tab.</Notice>;
  }

  return (
    <div className="sl-backtest">
      <div className="sl-bt-layout">
        <aside className="sl-card sl-config" aria-label="Backtest configuration">
          <h3>Configuration</h3>
          <section className="sl-form-section" aria-label="Strategy">
            <h4>Strategy</h4>
            <Field label="Strategy"><StyledSelect ariaLabel="Strategy" value={sid} onChange={setSid} options={intraday.map((s) => ({ value: s.id, label: `${s.name} (v${s.current_version})` }))} /></Field>
            <div className="sl-row">
              <Field label="Version"><StyledSelect ariaLabel="Version" value={String(version ?? '')} onChange={(v) => setVersion(Number(v))} options={(detail?.versions ?? []).map((v) => ({ value: String(v.version), label: `v${v.version}` }))} /></Field>
              <Field label="Analysis"><StyledSelect ariaLabel="Analysis type" value={kind} onChange={(v) => setKind(v as Kind)} options={(Object.keys(KIND_LABEL) as Kind[]).filter((k) => !isEngine || k === 'single' || k === 'walk_forward').map((k) => ({ value: k, label: isEngine && k === 'walk_forward' ? 'Walk-forward (phases 0-2 + controls)' : KIND_LABEL[k] }))} /></Field>
            </div>
          </section>
          <section className="sl-form-section" aria-label="Instruments">
            <h4>Instruments ({chosen.length})</h4>
            <InstrumentPicker stored={instruments} market={market} chosen={chosen} onChange={setChosen} window={{ start, end }} onStoredChanged={() => onStoredChanged?.()} />
          </section>
          <section className="sl-form-section" aria-label="Window">
            <h4>Window</h4>
            <div className="sl-row"><Field label="From"><input type="date" value={start} onChange={(e) => setStart(e.target.value)} /></Field><Field label="To"><input type="date" value={end} onChange={(e) => setEnd(e.target.value)} /></Field></div>
            {isEngine && kind === 'walk_forward' && <label className="sl-check"><input type="checkbox" checked={quick} onChange={(e) => setQuick(e.target.checked)} /> Quick grid (smoke test, not for results)</label>}
            {kind === 'walk_forward' && !isEngine && <div className="sl-row"><Field label="Train days"><input type="number" min={2} value={options.train_days} onChange={(e) => setOptions({ ...options, train_days: e.target.value })} /></Field><Field label="Test days"><input type="number" min={1} value={options.test_days} onChange={(e) => setOptions({ ...options, test_days: e.target.value })} /></Field></div>}
            {kind === 'is_oos' && <Field label="In-sample share"><input type="number" min={0.3} max={0.9} step="0.05" value={options.split_ratio} onChange={(e) => setOptions({ ...options, split_ratio: e.target.value })} /></Field>}
            {(kind === 'walk_forward' || kind === 'sensitivity') && !isEngine && (
              <Field label="Parameters to vary" hint="e.g. fast.length, slow.length (max 3). Blank = first three.">
                <input list="tunables" value={options.parameters} onChange={(e) => setOptions({ ...options, parameters: e.target.value })} />
                <datalist id="tunables">{detail?.tunable_parameters?.map((t) => <option key={t.key} value={t.key} />)}</datalist>
              </Field>
            )}
          </section>
          <section className="sl-form-section" aria-label="Capital and risk">
            <h4>Capital &amp; risk</h4>
            <div className="sl-row"><Field label="Capital"><input inputMode="decimal" value={capital} onChange={(e) => setCapital(e.target.value)} /></Field><Field label="Leverage ×"><input inputMode="decimal" value={leverage} onChange={(e) => setLeverage(e.target.value)} /></Field></div>
            <div className="sl-row"><Field label="Max positions"><input type="number" min={1} value={maxConc} onChange={(e) => setMaxConc(e.target.value)} /></Field>
              <Field label="Direction"><StyledSelect ariaLabel="Direction" value={direction} onChange={setDirection} options={[{ value: 'both', label: 'Long + short' }, { value: 'long', label: 'Long only' }, { value: 'short', label: 'Short only' }]} /></Field></div>
            <Field label="Refuse if missing 1m candles exceed (%)"><input type="number" min={0} max={100} step="0.5" value={maxMissing} onChange={(e) => setMaxMissing(e.target.value)} /></Field>
          </section>
          <section className="sl-form-section" aria-label="Costs">
            <h4>Costs</h4>
            <Field label="Slippage bps"><input inputMode="decimal" value={slip} onChange={(e) => setSlip(e.target.value)} /></Field>
            <Field label="Cost profile"><StyledSelect ariaLabel="Cost profile" value={profile || activeProfile?.id || ''} onChange={setProfile} options={profiles.map((p) => ({ value: p.id, label: p.label }))} /></Field>
            {activeProfile && <p className="sl-muted">Assumed (not broker-confirmed) from {activeProfile.effective_from}. {activeProfile.source_note}</p>}
            <details className="sl-details"><summary>Override cost parameters</summary>
              <div className="sl-row sl-wrap">{activeProfile && Object.entries(activeProfile.params).map(([k, v]) => (
                <Field key={k} label={k.replace(/_/g, ' ')}><input aria-label={`override ${k}`} inputMode="decimal" placeholder={v} value={overrides[k] ?? ''} onChange={(e) => setOverrides({ ...overrides, [k]: e.target.value })} /></Field>
              ))}</div>
            </details>
          </section>
          <div className={`sl-preflight${pre.rows.length && !pre.canRun && !pre.rows.some((r) => r.state === 'checking') ? ' sl-preflight-bad' : ''}`} role="status" aria-label="Pre-flight data check">{pre.summary}</div>
          <div className="sl-row sl-actions">
            <button type="button" className="sl-primary sl-run" disabled={busy || !chosen.length || !pre.canRun} onClick={start_}>{busy ? 'Running…' : 'Run backtest'}</button>
            <button type="button" onClick={checkQuality} disabled={!chosen.length}>Check data quality</button>
          </div>
          {quality && (
            <div className="sl-quality" aria-label="Data quality">
              {Object.entries(quality).map(([sym, q]) => {
                const r = q as { status: string; bars: number; missing_pct: number | null; issues: string[] };
                return <p key={sym}><strong>{sym}</strong> <StatusPill status={r.status} /> {r.bars?.toLocaleString()} bars · {r.missing_pct ?? '—'}% missing{r.issues?.length ? ` · ${r.issues.join('; ')}` : ''}</p>;
              })}
            </div>
          )}
          <p className="sl-disclaimer">{DISCLAIMER}</p>
        </aside>

        <div className="sl-results">
          {isEngine && <EngineParamsPanel stored={storedEngine} values={engineParams} onChange={setEngineParams} geometry={geo} />}
          {isOptions && <OptionsCoveragePanel underlying={def?.options?.underlying.underlying} />}
          {error && <Notice tone="bad">{error}</Notice>}
          {run && (run.status === 'queued' || run.status === 'running') && (
            <div className="sl-progress" role="progressbar" aria-valuenow={run.progress} aria-valuemin={0} aria-valuemax={100} aria-label="Backtest progress"><div style={{ width: `${Math.max(8, run.progress)}%` }} /><span>{run.status} · evaluating 1-minute candles…</span></div>
          )}
          {run?.status === 'failed' && <Notice tone="bad">Backtest failed: {run.error}</Notice>}
          {!run && !error && <PreRunPanel pre={pre} profile={activeProfile ?? null} overrides={overrides} slip={slip} maxMissing={maxMissing} window={{ start, end }} />}
          {run?.status === 'completed' && result && (result as { mode?: string }).mode === 'scalp_hold' && <ScalpHoldView r={result as unknown as ScalpHoldResult} />}
          {run?.status === 'completed' && result && (result as { mode?: string }).mode !== 'scalp_hold' && (
            <>
              <nav className="sl-subtabs" aria-label="Result sections">
                {(single ? ([...(single.options_report ? (['options'] as const) : []), 'summary', 'equity', 'monthly', 'trades', 'breakdown', 'assumptions', 'compare'] as const) : (['validation', 'assumptions', 'compare'] as const)).map((v) => (
                  <button key={v} type="button" className={(view === v || (!single && v === 'validation' && view === 'summary')) ? 'active' : ''} onClick={() => setView(v)}>{v}</button>
                ))}
              </nav>
              <p className="sl-muted">{run.kind !== 'single' ? KIND_LABEL[run.kind as Kind] : 'Single run'} · strategy v{run.strategy_version} · engine {run.engine_version} · indicators {run.indicator_version} · definition <code>{run.definition_hash.slice(0, 10)}</code></p>
              {single && single.options_report && view === 'options' && <OptionsReportView r={single.options_report} />}
              {single && view === 'summary' && <Summary r={single} />}
              {single && view === 'equity' && (
                <div className="sl-card"><h4>Equity curve</h4><LineChart points={single.equity_curve.map((p) => ({ t: p.t, v: p.equity }))} label="Equity (mark-to-market)" fill />
                  <h4>Drawdown</h4><LineChart points={single.equity_curve.map((p) => ({ t: p.t, v: p.drawdown_pct }))} label="Drawdown %" color="var(--sl-neg)" fill maxY={0} format={(v) => `${v.toFixed(2)}%`} /></div>
              )}
              {single && view === 'monthly' && <div className="sl-card"><h4>Monthly net P&amp;L</h4><BarChart label="Net P&L by month" bars={single.monthly.map((m) => ({ key: m.month, v: Number(m.net_pnl) }))} format={(v) => fmtMoney(v)} />
                <Scroll label="Monthly returns"><table className="sl-table sl-compact"><thead><tr><th>Month</th><th>Net P&amp;L</th><th>% of initial capital</th></tr></thead><tbody>{single.monthly.map((m) => <tr key={m.month}><td>{m.month}</td><td className={Number(m.net_pnl) >= 0 ? 'sl-r sl-pos' : 'sl-r sl-neg'}>{fmtMoney(m.net_pnl)}</td><td className="sl-r">{fmtPct(m.return_pct_of_initial)}</td></tr>)}</tbody></table></Scroll></div>}
              {single && view === 'trades' && <Ledger trades={single.trades} />}
              {single && view === 'breakdown' && <Breakdown r={single} />}
              {!single && <Validation r={result} />}
              {view === 'assumptions' && <Assumptions a={(result.assumptions as Record<string, unknown>) ?? { note: 'See configuration of the individual runs.' }} config={run.config} dataset={single?.dataset} />}
              {view === 'compare' && (
                <div className="sl-card">
                  <h4>Compare runs</h4>
                  <p className="sl-muted">Pick up to 4 single-run backtests of any strategy.</p>
                  <div className="sl-chips">{history.filter((h) => h.status === 'completed' && h.kind === 'single').map((h) => (
                    <label key={h.id} className="sl-check"><input type="checkbox" checked={compare.includes(h.id)} disabled={!compare.includes(h.id) && compare.length >= 4}
                      onChange={(e) => loadCompare(e.target.checked ? [...compare, h.id] : compare.filter((x) => x !== h.id))} /> v{h.strategy_version} · {fmtTime(h.created_at)}</label>
                  ))}</div>
                  {compareRows.length > 0 && <Comparison rows={compareRows} names={Object.fromEntries(strategies.map((s) => [s.id, s.name]))} />}
                </div>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}

const COMPONENT_LABEL: Record<string, string> = { brokerage: 'Brokerage', stt: 'STT', exchange_txn: 'Exchange charges', sebi: 'SEBI charges', ipft: 'IPFT', stamp_duty: 'Stamp duty', gst: 'GST', dp_charges: 'DP charges', other: 'Other / exchange fee' };

function CostBreakdownTable({ t }: { t: Trade }) {
  const b = t.cost_breakdown;
  if (!b) return <p className="sl-muted">No itemised breakdown stored for this trade.</p>;
  const pct = Number(b.pct_of_turnover);
  const gross = Number(t.gross_pnl);
  const net = Number(t.net_pnl);
  return (
    <table className="sl-table sl-compact sl-cost" aria-label="Cost breakdown">
      <tbody>
        <tr><th scope="row">Gross P&amp;L (before slippage &amp; charges)</th><td className="sl-r">{fmtMoney(t.gross_pnl)}</td></tr>
        <tr><th scope="row">Slippage (model, separate)</th><td className="sl-r">{fmtMoney(t.slippage_cost)}</td></tr>
        {(['brokerage', 'stt', 'exchange_txn', 'sebi', 'ipft', 'stamp_duty', 'gst'] as const).map((k) => <tr key={k}><th scope="row">{COMPONENT_LABEL[k]}</th><td className="sl-r">{fmtMoney(b[k])}</td></tr>)}
        {Number(b.dp_charges) !== 0 && <tr><th scope="row">DP charges</th><td className="sl-r">{fmtMoney(b.dp_charges)}</td></tr>}
        {Number(b.other) !== 0 && <tr><th scope="row">Other</th><td className="sl-r">{fmtMoney(b.other)}</td></tr>}
        <tr><th scope="row">Total charges</th><td className="sl-r">{fmtMoney(b.total)}</td></tr>
        <tr><th scope="row">Net P&amp;L</th><td className={net >= 0 ? 'sl-r sl-pos' : 'sl-r sl-neg'}>{fmtMoney(t.net_pnl)}</td></tr>
        <tr><th scope="row">Buy / sell / total turnover</th><td className="sl-r">{fmtMoney(b.buy_turnover)} / {fmtMoney(b.sell_turnover)} / {fmtMoney(b.turnover)}</td></tr>
        <tr><th scope="row">Charges as % of turnover</th><td className="sl-r">{pct.toFixed(4)}%</td></tr>
        <tr><th scope="row">Brokerage per order</th><td className="sl-r">{b.orders.map((o) => `${o.order_id} ${fmtMoney(o.brokerage)}`).join(' · ')}</td></tr>
        <tr><th scope="row">Gross vs net</th><td className="sl-r">{gross >= 0 ? 'gross positive' : 'gross negative'} → {net >= 0 ? 'net positive' : 'net negative'}{gross > 0 && net <= 0 ? ' (costs wipe out the gross gain)' : ''}</td></tr>
      </tbody>
    </table>
  );
}

function Summary({ r }: { r: SingleResult }) {
  const m = r.metrics;
  const num = (k: string) => (m[k] === null || m[k] === undefined ? '—' : String(m[k]));
  const net = Number(m.net_profit);
  return (
    <div className="sl-card">
      {r.warnings.map((w, i) => <Notice key={i} tone="warn">{w}</Notice>)}
      {r.refused_instruments?.map((x) => <Notice key={x.symbol} tone="bad">{x.symbol} was refused: {x.reason}</Notice>)}
      <div className="sl-stats" aria-label="Key results">
        <Stat label="Gross P&L (before slippage & charges)" value={fmtMoney(m.gross_pnl_before_costs as string)} tone={Number(m.gross_pnl_before_costs) >= 0 ? 'pos' : 'neg'} />
        <Stat label="Slippage (model)" value={fmtMoney(m.total_slippage as string)} hint="Execution-price impact from the slippage model; separate from statutory charges" />
        <Stat label="Charges (brokerage + statutory)" value={fmtMoney(m.total_charges as string)} hint="Brokerage, STT, exchange, SEBI, IPFT, stamp duty and GST from the central Dhan fee schedule. Income tax is not included." />
        <Stat label="Net profit (after slippage & charges)" value={fmtMoney(m.net_profit as string)} tone={net >= 0 ? 'pos' : 'neg'} />
        <Stat label="Return" value={fmtPct(m.return_pct as number)} tone={net >= 0 ? 'pos' : 'neg'} />
        <Stat label="Trades" value={num('total_trades')} /><Stat label="Win rate" value={m.win_rate == null ? '—' : fmtPct(Number(m.win_rate) * 100, 1)} />
        <Stat label="Profit factor (net)" value={num('profit_factor_net')} /><Stat label="Expectancy / trade" value={fmtMoney(m.expectancy_per_trade as string)} />
        <Stat label="Max drawdown" value={fmtPct(m.max_drawdown_pct as number)} tone="neg" /><Stat label="Avg hold (min)" value={num('avg_holding_minutes')} />
      </div>
      <dl className="sl-kv" aria-label="Detailed metrics">
        {[['Gross P&L before costs', 'gross_pnl_before_costs'], ['Gross profit (before costs)', 'gross_profit'], ['Gross loss (before costs)', 'gross_loss'], ['Slippage', 'total_slippage'], ['Charges', 'total_charges'], ['Net profit', 'net_profit'],
          ['Winning / losing', ''], ['Average win', 'avg_win'], ['Average loss', 'avg_loss'], ['Avg risk/reward', 'avg_risk_reward'], ['Avg R multiple', 'avg_r_multiple'], ['Largest win', 'largest_win'], ['Largest loss', 'largest_loss'],
          ['Max consecutive wins', 'max_consecutive_wins'], ['Max consecutive losses', 'max_consecutive_losses'], ['Profit factor (gross)', 'profit_factor_gross'], ['Max drawdown amount', 'max_drawdown_amount'], ['Sharpe', 'sharpe_ratio'], ['Sortino', 'sortino_ratio']]
          .map(([label, key]) => <div key={label}><dt>{label}</dt><dd>{key ? num(key) : `${m.winning_trades} / ${m.losing_trades}`}</dd></div>)}
      </dl>
      {m.cost_components && (
        <section aria-label="Charges breakdown">
          <h4>Charges breakdown (all trades)</h4>
          <dl className="sl-kv">
            {Object.entries(m.cost_components as unknown as Record<string, string>).filter(([, v]) => Number(v) !== 0).map(([k, v]) => <div key={k}><dt>{COMPONENT_LABEL[k] ?? k}</dt><dd>{fmtMoney(v)}</dd></div>)}
            <div><dt>Total charges</dt><dd>{fmtMoney(m.total_charges as string)}</dd></div>
            <div><dt>Buy turnover</dt><dd>{fmtMoney(m.total_buy_turnover as string)}</dd></div>
            <div><dt>Sell turnover</dt><dd>{fmtMoney(m.total_sell_turnover as string)}</dd></div>
            <div><dt>Total turnover</dt><dd>{fmtMoney(m.total_turnover as string)}</dd></div>
            <div><dt>Charges / turnover</dt><dd>{m.charges_pct_of_turnover == null ? '—' : `${Number(m.charges_pct_of_turnover).toFixed(4)}%`}</dd></div>
          </dl>
        </section>
      )}
      <p className="sl-muted">Sharpe/Sortino: {String(m.ratios_note)}</p>
      <p className="sl-muted">{r.benchmark.label}: {r.benchmark.average_return_pct == null ? '—' : fmtPct(r.benchmark.average_return_pct)} (average across instruments)</p>
      {r.trade_distribution && <><h4>Trade distribution (net P&amp;L)</h4><BarChart label="Trades per P&L bucket" bars={r.trade_distribution.map((b) => ({ key: `${b.from.toFixed(0)}…${b.to.toFixed(0)}`, v: b.count }))} format={(v) => `${v} trades`} /></>}
      {Object.keys(r.skipped_signals).length > 0 && <p className="sl-muted">Signals not traded: {Object.entries(r.skipped_signals).map(([k, v]) => `${k.replace(/_/g, ' ')} ×${v}`).join(', ')}</p>}
    </div>
  );
}

function OptionsReportView({ r }: { r: OptionsReport }) {
  const tbl = (title: string, g: Group2) => (
    <section className="sl-card"><h4>{title}</h4>
      {Object.keys(g).length ? <Scroll label={title}><table className="sl-table sl-compact"><thead><tr><th>Group</th><th className="sl-r">Trades</th><th className="sl-r">Net P&amp;L</th><th className="sl-r">Win rate</th><th className="sl-r">Expectancy</th></tr></thead><tbody>
        {Object.entries(g).map(([k, v]) => <tr key={k}><td>{k}</td><td className="sl-r">{v.trades}</td><td className={Number(v.net_pnl) >= 0 ? 'sl-r sl-pos' : 'sl-r sl-neg'}>{fmtMoney(v.net_pnl)}</td><td className="sl-r">{v.win_rate == null ? '—' : fmtPct(v.win_rate * 100, 1)}</td><td className="sl-r">{fmtMoney(v.expectancy)}</td></tr>)}
      </tbody></table></Scroll> : <p className="sl-muted">No trades.</p>}</section>
  );
  const indicative = r.price_basis.startsWith('INDICATIVE');
  return (
    <div className="sl-card" aria-label="Options performance report">
      <Notice tone={indicative ? 'bad' : 'info'}>{indicative ? 'INDICATIVE RESULT. ' : 'Price basis: '}{r.price_basis}</Notice>
      <div className="sl-stats">
        <Stat label="CE trades" value={r.ce_trades} /><Stat label="PE trades" value={r.pe_trades} />
        <Stat label="Avg entry spread" value={r.avg_entry_spread_pct == null ? '—' : `${r.avg_entry_spread_pct}%`} /><Stat label="Avg exit spread" value={r.avg_exit_spread_pct == null ? '—' : `${r.avg_exit_spread_pct}%`} />
        <Stat label="Avg |delta| at entry" value={r.avg_delta_at_entry ?? '—'} /><Stat label="Avg MAE" value={r.avg_mae_pct == null ? '—' : `${r.avg_mae_pct}%`} tone="neg" /><Stat label="Avg MFE" value={r.avg_mfe_pct == null ? '—' : `${r.avg_mfe_pct}%`} tone="pos" />
        <Stat label="Max hold (min)" value={r.max_holding_minutes ?? '—'} />
      </div>
      <p className="sl-muted">Greeks: {Object.entries(r.greeks_sources).map(([k, v]) => `${k} ${v}`).join(' · ')}. Signals {r.signals.total}, traded {r.signals.traded}.</p>
      {Object.keys(r.signals.no_trade_reasons).length > 0 && (
        <section aria-label="No-trade reasons"><h4>Why signals did not trade</h4><ul className="sl-plain">{Object.entries(r.signals.no_trade_reasons).map(([k, v]) => <li key={k}>{k.replace(/_/g, ' ')}: {v}</li>)}</ul></section>
      )}
      <div className="sl-grid-3">{tbl('CE vs PE', r.by_option_type)}{tbl('By delta bucket', r.by_delta_bucket)}{tbl('By expiry', r.by_expiry)}{tbl('By time of day (IST)', r.by_hour_ist)}{tbl('By stop / target configuration', r.by_config)}{tbl('By price source', r.by_price_source)}</div>
    </div>
  );
}

function Ledger({ trades }: { trades: Trade[] }) {
  const [sort, setSort] = useState<'entry_time' | 'net_pnl'>('entry_time');
  const [desc, setDesc] = useState(false);
  const [openIdx, setOpenIdx] = useState<number | null>(null);
  const rows = useMemo(() => trades.map((t, i) => ({ t, i })).sort((a, b) => {
    const x = sort === 'net_pnl' ? Number(a.t.net_pnl) - Number(b.t.net_pnl) : a.t.entry_time.localeCompare(b.t.entry_time);
    return desc ? -x : x;
  }), [trades, sort, desc]);
  const head = (key: 'entry_time' | 'net_pnl', label: string) => <th><button type="button" className="sl-link" aria-label={`Sort by ${label}`} onClick={() => { if (sort === key) setDesc(!desc); else { setSort(key); setDesc(false); } }}>{label}{sort === key ? (desc ? ' ↓' : ' ↑') : ''}</button></th>;
  if (!trades.length) return <Notice tone="info">No trades in this window.</Notice>;
  return (
    <div className="sl-card">
      <Scroll label="Trade ledger">
        <table className="sl-table sl-compact">
          <thead><tr><th>#</th><th>Instrument</th><th>Side</th>{head('entry_time', 'Entry')}<th>Exit</th><th className="sl-r">Qty</th><th className="sl-r">Entry fill</th><th className="sl-r">Exit fill</th><th>Reason</th><th className="sl-r">Gross</th><th className="sl-r">Slippage</th><th className="sl-r">Charges</th>{head('net_pnl', 'Net')}<th className="sl-r">R</th></tr></thead>
          <tbody>
            {rows.map(({ t, i }) => (
              <Fragment key={i}>
                <tr onClick={() => setOpenIdx(openIdx === i ? null : i)} className="sl-click" aria-expanded={openIdx === i}>
                  <td>{i + 1}</td><td>{t.symbol}</td><td className={t.side === 'long' ? 'sl-pos' : 'sl-neg'}>{t.side}</td><td>{fmtTime(t.entry_time)}</td><td>{fmtTime(t.exit_time)}</td>
                  <td className="sl-r">{t.qty}</td><td className="sl-r">{t.entry_fill}</td><td className="sl-r">{t.exit_fill}</td>
                  <td>{t.exit_reason.replace(/_/g, ' ')}{t.ambiguous_stop_and_target_same_bar && <Pill tone="warn" title="Stop and target touched in the same 1-minute candle; stop assumed first">ambiguous</Pill>}</td>
                  <td className="sl-r">{fmtMoney(t.gross_pnl)}</td><td className="sl-r">{fmtMoney(t.slippage_cost)}</td><td className="sl-r" title="Brokerage + statutory charges (hover or expand the row for the itemised breakdown)">{fmtMoney(t.charges)}</td>
                  <td className={Number(t.net_pnl) >= 0 ? 'sl-r sl-pos' : 'sl-r sl-neg'}>{fmtMoney(t.net_pnl)}</td><td className="sl-r">{t.r_multiple ?? '—'}</td>
                </tr>
                {openIdx === i && (
                  <tr className="sl-detail"><td colSpan={14}>
                    <dl className="sl-dl">
                      <dt>Signal (candle close)</dt><dd>{fmtTime(t.signal_time)}</dd><dt>Raw entry / exit</dt><dd>{t.entry_raw} → {t.exit_raw}</dd>
                      <dt>Stop / target (simulation)</dt><dd>{t.stop_initial ?? '—'} / {t.target ?? '—'}</dd><dt>Held</dt><dd>{t.holding_minutes} min</dd>
                    </dl>
                    {t.option && (
                      <dl className="sl-dl" aria-label="Option trade details">
                        <dt>Contract</dt><dd>{t.symbol} · {t.option.option_type} {t.option.strike} · expiry {t.option.expiry} · {t.option.lots} lot(s) × {t.option.lot_size}</dd>
                        <dt>Delta</dt><dd>{t.option.delta == null ? '—' : t.option.delta.toFixed(3)} ({t.option.delta_source})</dd>
                        <dt>Entry bid / ask / spread</dt><dd>{t.option.entry_bid} / {t.option.entry_ask} / {t.option.entry_spread_pct ?? '—'}%</dd>
                        <dt>Exit bid / ask / spread</dt><dd>{t.option.exit_bid} / {t.option.exit_ask} / {t.option.exit_spread_pct ?? '—'}%</dd>
                        <dt>Price source</dt><dd>{t.option.price_source === 'quote' ? 'recorded option quotes' : 'INDICATIVE OHLC (estimated execution)'}</dd>
                        <dt>MAE / MFE</dt><dd>{t.option.mae_pct}% / {t.option.mfe_pct}%</dd>
                        <dt>Why this contract</dt><dd>{t.option.selection_why}</dd>
                        {t.option.gap_through_stop && <><dt>Note</dt><dd>gapped through the stop</dd></>}
                      </dl>
                    )}
                    <CostBreakdownTable t={t} />
                    {t.explain && <ul className="sl-explain">{t.explain.map((c, k) => <li key={k} style={{ paddingLeft: c.depth * 12 }}>{c.satisfied ? '✓' : '✗'} {c.label}{c.left != null ? ` (${c.left} vs ${c.right})` : ''}</li>)}</ul>}
                  </td></tr>
                )}
              </Fragment>
            ))}
          </tbody>
        </table>
      </Scroll>
    </div>
  );
}

function Breakdown({ r }: { r: SingleResult }) {
  const tbl = (title: string, g: SingleResult['by_side']) => (
    <section className="sl-card"><h4>{title}</h4>
      {Object.keys(g).length ? <Scroll label={title}><table className="sl-table sl-compact"><thead><tr><th>Group</th><th className="sl-r">Trades</th><th className="sl-r">Net P&amp;L</th><th className="sl-r">Win rate</th><th className="sl-r">Expectancy</th></tr></thead><tbody>
        {Object.entries(g).map(([k, v]) => <tr key={k}><td>{k}</td><td className="sl-r">{v.trades}</td><td className={Number(v.net_pnl) >= 0 ? 'sl-r sl-pos' : 'sl-r sl-neg'}>{fmtMoney(v.net_pnl)}</td><td className="sl-r">{v.win_rate == null ? '—' : fmtPct(v.win_rate * 100, 1)}</td><td className="sl-r">{fmtMoney(v.expectancy)}</td></tr>)}
      </tbody></table></Scroll> : <p className="sl-muted">No trades.</p>}</section>
  );
  return <div className="sl-grid-3">{tbl('Long vs short', r.by_side)}{tbl('By instrument', r.by_instrument)}{tbl('By trading session', r.by_session)}
    <section className="sl-card"><h4>Exit reasons</h4><ul className="sl-plain">{Object.entries(r.exit_reasons).map(([k, v]) => <li key={k}>{k.replace(/_/g, ' ')}: {v}</li>)}</ul></section></div>;
}

function Validation({ r }: { r: Record<string, unknown> }) {
  const mode = r.mode as string;
  const rows = (r.folds as { train: string[]; test: string[]; chosen_params: Record<string, number>; train_result: Record<string, unknown>; test_result: Record<string, unknown> }[] | undefined);
  const grid = r.grid as { params: Record<string, number>; total_trades: number; net_profit: string; return_pct: number; profit_factor_net: number | null; max_drawdown_pct: number }[] | undefined;
  return (
    <div className="sl-card">
      <Notice tone="info">{String(r.note ?? '')}</Notice>
      {mode === 'in_out_of_sample' && (
        <Scroll label="In-sample vs out-of-sample"><table className="sl-table"><thead><tr><th>Window</th><th className="sl-r">Trades</th><th className="sl-r">Net profit</th><th className="sl-r">Return</th><th className="sl-r">Profit factor</th><th className="sl-r">Max DD</th></tr></thead><tbody>
          {(['in_sample', 'out_of_sample'] as const).map((k) => { const h = r[k] as Record<string, number | string | null>; return <tr key={k}><td>{k.replace(/_/g, ' ')}</td><td className="sl-r">{String(h.total_trades)}</td><td className="sl-r">{fmtMoney(h.net_profit as string)}</td><td className="sl-r">{fmtPct(h.return_pct as number)}</td><td className="sl-r">{String(h.profit_factor_net ?? '—')}</td><td className="sl-r">{fmtPct(h.max_drawdown_pct as number)}</td></tr>; })}
        </tbody></table></Scroll>
      )}
      {mode === 'walk_forward' && (
        <>
          <p>Out-of-sample total: <strong>{String(r.oos_total_trades)}</strong> trades, net <strong>{fmtMoney(r.oos_net_profit as number)}</strong>. Optimised on {String(r.optimised_on)}.</p>
          <Scroll label="Walk-forward folds"><table className="sl-table sl-compact"><thead><tr><th>Train</th><th>Test</th><th>Chosen (train only)</th><th className="sl-r">Train net</th><th className="sl-r">Test trades</th><th className="sl-r">Test net</th></tr></thead><tbody>
            {rows?.map((f, i) => <tr key={i}><td>{f.train[0].slice(0, 10)} → {f.train[1].slice(0, 10)}</td><td>{f.test[0].slice(0, 10)} → {f.test[1].slice(0, 10)}</td><td>{JSON.stringify(f.chosen_params)}</td><td className="sl-r">{fmtMoney(f.train_result.net_profit as string)}</td><td className="sl-r">{String(f.test_result.total_trades)}</td><td className="sl-r">{fmtMoney(f.test_result.net_profit as string)}</td></tr>)}
          </tbody></table></Scroll>
        </>
      )}
      {mode === 'sensitivity' && (
        <Scroll label="Parameter sensitivity"><table className="sl-table sl-compact"><thead><tr><th>Parameters</th><th className="sl-r">Trades</th><th className="sl-r">Net profit</th><th className="sl-r">Return</th><th className="sl-r">PF</th><th className="sl-r">Max DD</th></tr></thead><tbody>
          {grid?.map((g, i) => <tr key={i}><td>{Object.entries(g.params).map(([k, v]) => `${k}=${v}`).join(', ')}</td><td className="sl-r">{g.total_trades}</td><td className="sl-r">{fmtMoney(g.net_profit)}</td><td className="sl-r">{fmtPct(g.return_pct)}</td><td className="sl-r">{g.profit_factor_net ?? '—'}</td><td className="sl-r">{fmtPct(g.max_drawdown_pct)}</td></tr>)}
        </tbody></table></Scroll>
      )}
    </div>
  );
}

function Assumptions({ a, config, dataset }: { a: Record<string, unknown>; config: Record<string, unknown>; dataset?: SingleResult['dataset'] }) {
  return (
    <div className="sl-card">
      <Notice tone="warn">Costs and fills are modelling assumptions, not broker-confirmed values.</Notice>
      <Scroll label="Execution assumptions"><table className="sl-table sl-compact"><tbody>
        {Object.entries(a).map(([k, v]) => <tr key={k}><th scope="row">{k.replace(/_/g, ' ')}</th><td>{typeof v === 'object' && v !== null ? <code>{JSON.stringify(v)}</code> : String(v)}</td></tr>)}
      </tbody></table></Scroll>
      {dataset && (<><h4>Data provenance</h4><p className="sl-muted">Dataset SHA-256 <code>{dataset.sha256.slice(0, 16)}…</code>. Same data + configuration reproduces the same result.</p>
        <Scroll label="Datasets"><table className="sl-table sl-compact"><thead><tr><th>Instrument</th><th className="sl-r">1m bars</th><th>From</th><th>To</th><th>Quality</th></tr></thead><tbody>
          {dataset.instruments.map((d) => <tr key={d.symbol}><td>{d.symbol}</td><td className="sl-r">{d.bars.toLocaleString()}</td><td>{d.start?.slice(0, 16)}</td><td>{d.end?.slice(0, 16)}</td><td><StatusPill status={String(d.quality.status ?? 'ok')} /> {Array.isArray(d.quality.issues) && (d.quality.issues as string[]).join('; ')}</td></tr>)}
        </tbody></table></Scroll></>)}
      <details className="sl-details"><summary>Run configuration</summary><pre className="sl-code">{JSON.stringify(config, null, 2)}</pre></details>
    </div>
  );
}

function Comparison({ rows, names }: { rows: RunRow[]; names: Record<string, string> }) {
  const keys: [string, string][] = [['total_trades', 'Trades'], ['win_rate', 'Win rate'], ['net_profit', 'Net profit'], ['return_pct', 'Return %'], ['profit_factor_net', 'Profit factor'], ['expectancy_per_trade', 'Expectancy'], ['max_drawdown_pct', 'Max DD %'], ['avg_holding_minutes', 'Avg hold']];
  return (
    <Scroll label="Strategy comparison"><table className="sl-table sl-compact"><thead><tr><th>Metric</th>{rows.map((r) => <th key={r.id} className="sl-r">{names[r.strategy_id] ?? 'strategy'} v{r.strategy_version}</th>)}</tr></thead><tbody>
      {keys.map(([k, label]) => <tr key={k}><th scope="row">{label}</th>{rows.map((r) => <td key={r.id} className="sl-r">{r.metrics?.[k] == null ? '—' : String(r.metrics[k])}</td>)}</tr>)}
    </tbody></table></Scroll>
  );
}


/** What the right pane shows before any run: the selected instruments' data quality and the resolved cost assumptions. */
function PreRunPanel({ pre, profile, overrides, slip, maxMissing, window: win }: {
  pre: ReturnType<typeof preflight>; profile: { label: string; effective_from: string; source_note: string; params: Record<string, string> } | null;
  overrides: Record<string, string>; slip: string; maxMissing: string; window: { start: string; end: string };
}) {
  return (
    <div className="sl-prerun">
      <section className="sl-card" aria-label="Selected instruments data quality">
        <h3>Data quality · {win.start} → {win.end}</h3>
        {pre.rows.length === 0 ? <p className="sl-muted">Pick instruments on the left. Their stored 1-minute data is checked here before you run, against the {maxMissing}% missing-candle limit.</p> : (
          <Scroll label="Instrument data quality">
            <table className="sl-table sl-compact">
              <thead><tr><th>Instrument</th><th className="sl-r">Bars</th><th className="sl-r">Missing</th><th>Verdict</th></tr></thead>
              <tbody>{pre.rows.map((r) => (
                <tr key={r.id}>
                  <td>{r.name}</td><td className="sl-r">{r.quality ? r.quality.bars.toLocaleString() : '—'}</td><td className="sl-r">{r.quality?.missing_pct != null ? `${r.quality.missing_pct}%` : '—'}</td>
                  <td>{r.state === 'checking' ? <span className="sl-muted">checking…</span> : r.state === 'eligible' ? <Pill tone="ok">will run</Pill> : <><Pill tone="bad">excluded</Pill> {r.reason}</>}</td>
                </tr>))}</tbody>
            </table>
          </Scroll>
        )}
      </section>
      <section className="sl-card" aria-label="Resolved cost assumptions">
        <h3>Cost assumptions</h3>
        <p className="sl-muted">{profile ? `${profile.label} · assumed, not broker-confirmed, effective from ${profile.effective_from}. ${profile.source_note}` : 'No cost profile for this market.'}</p>
        <dl className="sl-dl">
          <dt>Slippage</dt><dd>{slip || '—'} bps per side</dd>
          {profile && Object.entries(profile.params).map(([k, v]) => <><dt key={`${k}-k`}>{k.replace(/_/g, ' ')}</dt><dd key={`${k}-v`}>{overrides[k] ? `${overrides[k]} (your override; default ${v})` : v}</dd></>)}
        </dl>
        <p className="sl-muted">Results use only completed 1-minute candles, fill at the next candle’s open, and list every assumption. Missing candles are never filled.</p>
      </section>
    </div>
  );
}
