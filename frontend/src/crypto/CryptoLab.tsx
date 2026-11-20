import { useCallback, useEffect, useMemo, useState, type FormEvent } from 'react';
import { apiRequest } from '../api';
import { CryptoCandleChart, EquityChart, MarkerLegend, exitMarker, type TradeMarker } from './CryptoCharts';
import { DEFAULT_DAYS, RESOLUTIONS, messageOf, type Candles, type ContractInfo } from './cryptoApi';

type ParamSpec = { key: string; label: string; kind: 'int' | 'decimal' | 'bool'; default: string; minimum: string | null; maximum: string | null };
type StrategyDef = { id: string; name: string; description: string; params: ParamSpec[] };
type StrategiesResponse = { resolutions: string[]; max_days: Record<string, number>; strategies: StrategyDef[] };

type Trade = {
  side: 'long' | 'short'; contracts: string; entry_time: string; exit_time: string; entry_price: string; exit_price: string;
  gross_pnl: string; fees: string; net_pnl: string; exit_reason: string; bars_held: number;
  notional_usd?: string; margin_used?: string | null; leverage?: string | null;
};
type Run = {
  id: string; created_at: string; strategy_id: string; strategy_name: string; symbol: string; resolution: string;
  window_start: string; window_end: string; bars_used: number; status: string; hypothetical: boolean;
  metrics: Record<string, string | number | null>;
  params?: Record<string, string>; assumptions?: Record<string, unknown>;
  data_quality?: Record<string, unknown>; trades?: Trade[]; equity?: { time: string; equity: string }[]; label?: string;
};

type SizingMode = 'contracts' | 'notional_usd' | 'risk_pct';
const SIZING_LABEL: Record<SizingMode, string> = { contracts: 'Fixed contracts', notional_usd: 'USD notional per trade', risk_pct: '% of equity risked to the stop' };
const plain = (value: unknown) => (value === null || value === undefined || value === '' ? '—' : String(value));

const dash = (value: string | number | null | undefined, suffix = '') => (value === null || value === undefined ? '—' : `${value}${suffix}`);
const tone = (value: string | number | null | undefined) => (value === null || value === undefined ? undefined : Number(value) >= 0 ? 'positive' : 'negative');

type Props = { symbol: string; setSymbol: (symbol: string) => void; resolution: string; setResolution: (resolution: string) => void };

export function CryptoLab({ symbol, setSymbol, resolution, setResolution }: Props) {
  const [meta, setMeta] = useState<StrategiesResponse | null>(null);
  const [strategyId, setStrategyId] = useState('ema_cross');
  const [values, setValues] = useState<Record<string, string>>({});
  const [days, setDays] = useState(String(DEFAULT_DAYS['15m']));
  const [contracts, setContracts] = useState('10');
  const [contract, setContract] = useState<ContractInfo | null>(null);
  const [contractError, setContractError] = useState<string | null>(null);
  const [leverage, setLeverage] = useState('10');
  const [sizingMode, setSizingMode] = useState<SizingMode>('contracts');
  const [sizingValue, setSizingValue] = useState('1000');
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);
  const [capital, setCapital] = useState('1000');
  const [slippage, setSlippage] = useState('2');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [run, setRun] = useState<Run | null>(null);
  const [candles, setCandles] = useState<Candles | null>(null);
  const [history, setHistory] = useState<Run[]>([]);

  const strategy = meta?.strategies.find((item) => item.id === strategyId);

  const refreshHistory = useCallback(async () => {
    try { setHistory(await apiRequest<Run[]>('/crypto-delta/backtests?limit=20')); } catch { /* history is optional */ }
  }, []);

  useEffect(() => {
    apiRequest<StrategiesResponse>('/crypto-delta/strategies').then(setMeta).catch((e: unknown) => setError(messageOf(e)));
    void refreshHistory();
  }, [refreshHistory]);

  useEffect(() => {
    if (!strategy) return;
    setValues(Object.fromEntries(strategy.params.map((p) => [p.key, p.default])));
  }, [strategy]);

  useEffect(() => { setDays(String(DEFAULT_DAYS[resolution] ?? 30)); }, [resolution]);

  // Every perpetual has its own lot size, margin and leverage limits: read them from Delta's product data for the chosen contract.
  useEffect(() => {
    const wanted = symbol.trim().toUpperCase();
    if (wanted.length < 3) { setContract(null); return undefined; }
    let cancelled = false;
    const timer = window.setTimeout(() => {
      apiRequest<ContractInfo>(`/crypto-delta/contracts/${encodeURIComponent(wanted)}`)
        .then((info) => {
          if (cancelled) return;
          setContract(info); setContractError(null);
          const max = Number(info.max_leverage);
          setLeverage((current) => (Number.isFinite(max) && Number(current) > max ? String(max) : current));
        })
        .catch((e: unknown) => { if (!cancelled) { setContract(null); setContractError(messageOf(e)); } });
    }, 300);
    return () => { cancelled = true; window.clearTimeout(timer); };
  }, [symbol]);

  const [chartNote, setChartNote] = useState<string | null>(null);

  // Chart the run's own window. If its candles are no longer stored, download them again from
  // Delta's public history (completed candles only) instead of silently showing an empty chart.
  const loadCandlesFor = useCallback(async (result: Run) => {
    setChartNote(null);
    const query = `symbol=${encodeURIComponent(result.symbol)}&resolution=${result.resolution}&start=${encodeURIComponent(result.window_start)}&end=${encodeURIComponent(result.window_end)}&limit=5000`;
    const fetchStored = () => apiRequest<Candles>(`/crypto-delta/candles?${query}`);
    try {
      let stored = await fetchStored();
      if (stored.count < result.bars_used * 0.9) {
        setChartNote('Candles for this run are not stored; downloading them again from Delta…');
        try {
          await apiRequest('/crypto-delta/candles/sync', {
            method: 'POST',
            body: JSON.stringify({ symbol: result.symbol, resolution: result.resolution, start: result.window_start, end: result.window_end }),
          });
          stored = await fetchStored();
          setChartNote(stored.count > 0 ? 'Candles were re-downloaded from Delta for this run.' : 'No candles could be loaded for this run.');
        } catch (e) {
          setChartNote(`Candles for this run could not be re-downloaded: ${messageOf(e)}`);
        }
      }
      setCandles(stored.count > 0 ? stored : null);
    } catch (e) {
      setCandles(null);
      setChartNote(`Chart unavailable: ${messageOf(e)}`);
    }
  }, []);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!strategy) return;
    setBusy(true);
    setError(null);
    try {
      const params: Record<string, unknown> = {};
      for (const spec of strategy.params) {
        const raw = values[spec.key] ?? spec.default;
        params[spec.key] = spec.kind === 'bool' ? raw === 'true' : spec.kind === 'int' ? Number(raw) : raw.trim();
      }
      const result = await apiRequest<Run>('/crypto-delta/backtests', {
        method: 'POST',
        body: JSON.stringify({
          strategy_id: strategy.id, symbol: symbol.trim().toUpperCase(), resolution, days: Number(days), params,
          contracts: contracts.trim(), initial_capital: capital.trim(), slippage_bps: slippage.trim(), leverage: leverage.trim(),
          sizing_mode: sizingMode, ...(sizingMode === 'notional_usd' ? { notional_usd: sizingValue.trim() } : sizingMode === 'risk_pct' ? { risk_pct: sizingValue.trim() } : {}),
        }),
      });
      setRun(result);
      await Promise.all([loadCandlesFor(result), refreshHistory()]);
    } catch (e) {
      setError(messageOf(e));
      void refreshHistory();
    } finally {
      setBusy(false);
    }
  }

  async function remove(id: string) {
    setError(null);
    try {
      await apiRequest<void>(`/crypto-delta/backtests/${id}`, { method: 'DELETE' });
      setConfirmDelete(null);
      if (run?.id === id) { setRun(null); setCandles(null); }
      await refreshHistory();
    } catch (e) { setError(messageOf(e)); }
  }

  async function open(id: string) {
    try {
      const result = await apiRequest<Run>(`/crypto-delta/backtests/${id}`);
      setRun(result);
      setSymbol(result.symbol);
      await loadCandlesFor(result);
    } catch (e) { setError(messageOf(e)); }
  }

  const markers = useMemo<TradeMarker[]>(() => (run?.trades ?? []).flatMap((t) => [
    { time: t.entry_time, kind: 'entry' as const, side: t.side, text: t.side === 'long' ? 'L' : 'S' },
    { time: t.exit_time, kind: 'exit' as const, side: t.side, ...exitMarker(t.exit_reason) },
  ]), [run]);

  const m = run?.metrics;
  const q = run?.data_quality as Record<string, string | number | boolean | null | Record<string, number>> | undefined;
  const a = run?.assumptions as Record<string, unknown> | undefined;
  const marginOf = (x: Record<string, unknown>) => x.margin as Record<string, string | number | null> | undefined;
  const paramLabel = (key: string) => strategy?.params.find((p) => p.key === key)?.label ?? key.replace(/_/g, ' ');
  const sizeText = (x: Record<string, unknown>) => (x.sizing_mode === 'notional_usd' ? `${plain(x.sizing_value as string)} USD notional per trade` : x.sizing_mode === 'risk_pct' ? `${plain(x.sizing_value as string)}% of equity risked to the stop` : `${plain((x.contracts_per_trade ?? x.sizing_value) as string)} contracts per trade`);

  return (
    <div className="crypto-lab">
      <section className="data-section" aria-label="Backtest setup">
        <div className="section-heading"><div><p className="eyebrow">STRATEGY LAB</p><h3>Backtest a perpetual strategy</h3></div></div>
        <p className="crypto-sub">Replays completed Delta candles only; signals are decided at a bar's close and filled at the next open. Fees come from the exchange's own contract data. Results are hypothetical and never mixed with real P&amp;L.</p>
        {strategy && <p className="crypto-sub"><strong>{strategy.name}.</strong> {strategy.description}</p>}
        <form className="crypto-form" onSubmit={(event) => void submit(event)}>
          <label>Strategy<select value={strategyId} onChange={(event) => setStrategyId(event.target.value)}>{meta?.strategies.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}</select></label>
          <label>Contract<input value={symbol} onChange={(event) => setSymbol(event.target.value.toUpperCase())} required /></label>
          <label>Candle size<select value={resolution} onChange={(event) => setResolution(event.target.value)}>{RESOLUTIONS.map((r) => <option key={r} value={r}>{r}</option>)}</select></label>
          <label>History (days)<input value={days} onChange={(event) => setDays(event.target.value)} inputMode="numeric" /></label>
          <label>Leverage (max {contract?.max_leverage ?? '—'}x)<input value={leverage} onChange={(event) => setLeverage(event.target.value)} inputMode="decimal" aria-label="Leverage" /></label>
          <label>Position size<select value={sizingMode} onChange={(event) => setSizingMode(event.target.value as SizingMode)} aria-label="Position size">{(Object.keys(SIZING_LABEL) as SizingMode[]).map((k) => <option key={k} value={k}>{SIZING_LABEL[k]}</option>)}</select></label>
          {sizingMode === 'contracts'
            ? <label>Contracts per trade<input value={contracts} onChange={(event) => setContracts(event.target.value)} inputMode="decimal" aria-label="Contracts per trade" /></label>
            : <label>{sizingMode === 'notional_usd' ? 'USD notional per trade' : '% of equity risked'}<input value={sizingValue} onChange={(event) => setSizingValue(event.target.value)} inputMode="decimal" aria-label="Size value" /></label>}
          <label>Starting capital (USD)<input value={capital} onChange={(event) => setCapital(event.target.value)} inputMode="decimal" /></label>
          <label>Slippage (bps)<input value={slippage} onChange={(event) => setSlippage(event.target.value)} inputMode="decimal" /></label>
          {strategy?.params.map((spec) => spec.kind === 'bool'
            ? <label key={spec.key} className="crypto-check"><input type="checkbox" checked={values[spec.key] === 'true'} onChange={(event) => setValues({ ...values, [spec.key]: String(event.target.checked) })} /> {spec.label}</label>
            : <label key={spec.key}>{spec.label}<input value={values[spec.key] ?? ''} onChange={(event) => setValues({ ...values, [spec.key]: event.target.value })} inputMode="decimal" title={`${spec.minimum ?? ''} – ${spec.maximum ?? ''}`} /></label>)}
          <button className="primary-button" type="submit" disabled={busy || !strategy}>{busy ? 'Running…' : 'Run backtest'}</button>
        </form>
        {contractError && <p className="crypto-sub" role="status">Contract data: {contractError}</p>}
        {contract && (
          <p className="crypto-sub" aria-label="Contract lot and margin">
            <strong>{contract.symbol}</strong>: 1 contract = <strong>{plain(contract.units_per_contract ?? contract.contract_value)}</strong> of the underlying · tick {plain(contract.tick_size)} · taker fee {(Number(contract.taker_fee_rate) * 100).toFixed(3)}%
            {contract.initial_margin_pct ? <> · initial margin {contract.initial_margin_pct}% (max leverage {plain(contract.max_leverage)}x) · maintenance margin {plain(contract.maintenance_margin_pct)}%</> : null}
            {contract.position_size_limit ? <> · position limit {contract.position_size_limit.toLocaleString()} contracts</> : null}. Margin per contract ≈ price × {plain(contract.units_per_contract ?? contract.contract_value)} ÷ leverage.
          </p>
        )}
        {error && <p className="error-message" role="alert">{error}</p>}
      </section>

      {run && m && (
        <section className="data-section" aria-label="Backtest result">
          <div className="section-heading"><div><p className="eyebrow">RESULT · {run.strategy_name} · {run.symbol} · {run.resolution}</p><h3>Hypothetical performance</h3></div>
            {confirmDelete === run.id
              ? <span role="group" aria-label="Confirm delete this run"><button className="text-button negative" type="button" onClick={() => void remove(run.id)}>Delete this run for good</button> <button className="text-button" type="button" onClick={() => setConfirmDelete(null)}>Keep</button></span>
              : <button className="text-button" type="button" onClick={() => setConfirmDelete(run.id)}>Delete this run</button>}</div>
          <p className="crypto-verdict crypto-verdict-hypo">{run.label ?? 'Hypothetical backtest. Not a position, an order, or real P&L.'}</p>
          <div className="crypto-kpis">
            <div><span>Net P&amp;L</span><strong className={tone(m.net_pnl)}>{dash(m.net_pnl)} USD</strong></div>
            <div><span>Return on capital</span><strong className={tone(m.return_pct)}>{dash(m.return_pct, '%')}</strong></div>
            <div><span>Trades</span><strong>{m.closed_trades}</strong></div>
            <div><span>Win rate</span><strong>{dash(m.win_rate_pct, '%')}</strong></div>
            <div><span>Profit factor</span><strong>{dash(m.profit_factor)}</strong></div>
            <div><span>Expectancy / trade</span><strong className={tone(m.expectancy)}>{dash(m.expectancy)}</strong></div>
            <div><span>Max drawdown</span><strong className="negative">{dash(m.max_drawdown)} ({dash(m.max_drawdown_pct, '%')})</strong></div>
            <div><span>Fees paid</span><strong>{dash(m.total_fees)}</strong></div>
            <div><span>Time in market</span><strong>{dash(m.exposure_pct, '%')}</strong></div>
            {m.liquidations !== undefined && <div><span>Liquidations</span><strong className={Number(m.liquidations) > 0 ? 'negative' : undefined}>{dash(m.liquidations)}</strong></div>}
            {m.skipped_entries_margin !== undefined && <div><span>Entries skipped (margin)</span><strong>{dash(m.skipped_entries_margin)}</strong></div>}
            {m.max_notional_usd !== undefined && <div><span>Largest position (USD)</span><strong>{dash(m.max_notional_usd)}</strong></div>}
          </div>
          {q && (
            <p className="crypto-sub">
              Data: {String(q.bars)} of {String(q.expected_bars)} completed candles ({String(q.coverage_pct)}% coverage), {String(q.gaps)} gap(s), {String(q.missing_bars)} missing bar(s)
              {q.downloaded ? ` · downloaded ${(q.download as Record<string, number>)?.inserted ?? 0} new candles from Delta` : ''}
              {q.download_error ? ` · download failed: ${String(q.download_error)}` : ''} · {String(q.source)}
            </p>
          )}
          <details className="crypto-details" open aria-label="Run settings">
            <summary>Settings this run used</summary>
            <dl className="crypto-settings">
              <div><dt>Strategy</dt><dd>{run.strategy_name}</dd></div>
              <div><dt>Contract</dt><dd>{run.symbol} · {run.resolution} candles</dd></div>
              <div><dt>Window</dt><dd>{new Date(run.window_start).toLocaleDateString()} → {new Date(run.window_end).toLocaleDateString()} ({run.bars_used} bars)</dd></div>
              {Object.entries(run.params ?? {}).map(([key, value]) => (
                <div key={key}><dt>{paramLabel(key)}</dt><dd>{value}</dd></div>
              ))}
              {a && <>
                <div><dt>Starting capital</dt><dd>{plain(a.initial_capital_usd)} USD</dd></div>
                <div><dt>Position size</dt><dd>{sizeText(a)}</dd></div>
                <div><dt>Leverage</dt><dd>{marginOf(a)?.leverage ? `${marginOf(a)?.leverage}x (max ${plain(marginOf(a)?.max_leverage)}x)` : 'not modelled'}</dd></div>
                <div><dt>Margin</dt><dd>{marginOf(a)?.initial_margin_pct ? `initial ${marginOf(a)?.initial_margin_pct}% · maintenance ${marginOf(a)?.maintenance_margin_pct}%` : '—'}</dd></div>
                <div><dt>Lot size</dt><dd>{plain(a.units_per_contract ?? a.contract_value)} per contract</dd></div>
                <div><dt>Fee / slippage</dt><dd>{plain(a.fee_rate_per_fill)} per fill · {plain(a.slippage_bps)} bps</dd></div>
              </>}
            </dl>
          </details>
          {chartNote && <p className="crypto-sub" role="status">{chartNote}</p>}
          {candles && candles.count > 0 && <CryptoCandleChart candles={candles.candles} markers={markers} height={380} />}
          {candles && candles.count > 0 && <MarkerLegend />}
          {run.equity && run.equity.length > 1 && <><h4 className="crypto-h4">Equity curve (hypothetical)</h4><EquityChart points={run.equity} /></>}
          {a && (
            <details className="crypto-details">
              <summary>Assumptions and what is not modelled</summary>
              <ul>
                <li>Fee per fill: {String(a.fee_rate_per_fill)} ({String(a.fee_source)})</li>
                <li>Contract value {String(a.contract_value)}, tick {String(a.tick_size)}, {sizeText(a)}, slippage {String(a.slippage_bps)} bps</li>
                {marginOf(a)?.model ? <li>Margin: {marginOf(a)?.model}; liquidation penalty factor {plain(marginOf(a)?.liquidation_penalty_factor)}</li> : null}
                <li>{String(a.fill_model)}</li>
                <li>Not modelled: {(a.not_modelled as string[] | undefined)?.join(', ')}</li>
              </ul>
            </details>
          )}
          {run.trades && run.trades.length > 0 && (
            <div className="table-scroll crypto-trades"><table>
              <thead><tr><th>Entry</th><th>Side</th><th>Entry price</th><th>Exit price</th><th>Net P&amp;L</th><th>Fees</th><th>Exit</th><th>Bars</th></tr></thead>
              <tbody>{run.trades.map((t, i) => (
                <tr key={`${t.entry_time}-${i}`}>
                  <td>{new Date(t.entry_time).toLocaleString()}</td><td>{t.side}</td><td>{t.entry_price}</td><td>{t.exit_price}</td>
                  <td className={tone(t.net_pnl)}>{t.net_pnl}</td><td>{t.fees}</td><td>{t.exit_reason}</td><td>{t.bars_held}</td>
                </tr>
              ))}</tbody>
            </table></div>
          )}
        </section>
      )}

      <section className="data-section" aria-label="Backtest history">
        <div className="section-heading"><div><p className="eyebrow">HISTORY</p><h3>Previous backtests</h3></div><span className="section-count">{history.length}</span></div>
        {history.length === 0 ? <p className="table-empty">No backtests yet.</p> : (
          <div className="table-scroll"><table>
            <thead><tr><th>Run</th><th>Strategy</th><th>Contract</th><th>Candles</th><th>Trades</th><th>Net P&amp;L</th><th>Status</th><th /><th /></tr></thead>
            <tbody>{history.map((h) => (
              <tr key={h.id}>
                <td>{new Date(h.created_at).toLocaleString()}</td><td>{h.strategy_name}</td><td>{h.symbol} · {h.resolution}</td><td>{h.bars_used}</td>
                <td>{dash(h.metrics.closed_trades)}</td><td className={tone(h.metrics.net_pnl)}>{dash(h.metrics.net_pnl)}</td><td>{h.status === 'completed' ? 'hypothetical' : h.status}</td>
                <td>{h.status === 'completed' && <button className="text-button" type="button" onClick={() => void open(h.id)}>Open</button>}</td>
                <td>{confirmDelete === h.id
                  ? <span role="group" aria-label="Confirm delete"><button className="text-button negative" type="button" onClick={() => void remove(h.id)}>Delete for good</button> <button className="text-button" type="button" onClick={() => setConfirmDelete(null)}>Keep</button></span>
                  : <button className="text-button" type="button" aria-label={`Delete ${h.strategy_name} ${h.symbol} run`} onClick={() => setConfirmDelete(h.id)}>Delete</button>}</td>
              </tr>
            ))}</tbody>
          </table></div>
        )}
      </section>
    </div>
  );
}
