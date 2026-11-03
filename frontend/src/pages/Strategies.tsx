import { useEffect, useState, type FormEvent } from 'react';
import { ApiRequestError, apiRequest } from '../api';
import { Panel } from '../components/ui';
import { DeltaResearchPage } from './DeltaResearch';

type Instrument = { id: string; symbol: string; display_name: string; currency: string };
type ParameterSchema = {
  name: string;
  label: string;
  type: 'integer' | 'number' | 'select';
  default: string | number | boolean | null;
  minimum?: number | null;
  maximum?: number | null;
  options: string[];
  optional: boolean;
};
type Registration = {
  strategy_id: string;
  display_name: string;
  description: string;
  asset_class: string;
  availability: {
    status: string;
    required_data: string[];
    limitations: string[];
    minimum_history: Record<string, number>;
  };
  parameters: ParameterSchema[];
};
type Params = Record<string, string | number | boolean | null> & { strategy_id?: string };
type Strategy = {
  id: string;
  instrument_id: string;
  name: string;
  description: string;
  status: 'suspended' | 'active' | 'retired';
  current_version: number;
  params: Params;
  disclaimer: string;
};
type Signal = {
  id: string;
  strategy_version: number;
  direction: 'entry' | 'exit';
  status: 'active' | 'rejected' | 'expired';
  rejection_reason: string | null;
  bar_timestamp: string;
  trigger_price: string;
  rationale: string;
  evidence: Record<string, unknown>;
  expires_at: string;
};
type DataAvailability = {
  instrument: string;
  interval: string;
  source: string;
  bars_required: number;
  bars_available: number;
  available: boolean;
};
type BacktestTrade = {
  decision_time: string;
  entry_time: string;
  entry_open: string;
  entry_fill: string;
  exit_time: string;
  exit_reason: string;
  exit_fill: string;
  gross_pnl: string;
  slippage_cost: string;
  brokerage_and_exchange_charges: string;
  total_costs: string;
  net_pnl: string;
  r_multiple: string;
  ambiguous_stop_target_same_bar: boolean;
};
type BacktestResult = {
  instrument: { symbol: string; currency: string };
  interval: string;
  source: string;
  data_window_start: string;
  data_window_end: string;
  bar_count: number;
  strategy_id: string;
  strategy_version: number;
  parameters: Params;
  assumptions: Record<string, string | number>;
  metrics: Record<string, string | number | null>;
  trades: BacktestTrade[];
  disclaimer: string;
};
type ParamForm = Record<string, string>;
type CostForm = {
  brokerage_bps: string;
  exchange_charges_bps: string;
  slippage_bps: string;
  stop_atr_multiple: string;
  target_r_multiple: string;
};

const rejectionLabels: Record<string, string> = {
  stale_bars: 'Latest bar is too old',
  synthetic_data: 'Synthetic price data is not usable',
  rsi_above_max: 'RSI filter not met',
};

function initialForm(schema: ParameterSchema[], values?: Params): ParamForm {
  return Object.fromEntries(schema.map((field) => {
    const value = values?.[field.name] ?? field.default;
    return [field.name, value === null || value === undefined ? '' : String(value)];
  }));
}

function toParams(schema: ParameterSchema[], form: ParamForm, strategyId: string): Params {
  const result: Params = { strategy_id: strategyId };
  for (const field of schema) {
    const value = form[field.name] ?? '';
    if (field.optional && value.trim() === '') {
      result[field.name] = null;
    } else if (field.type === 'integer' || field.type === 'number') {
      result[field.name] = Number(value);
    } else {
      result[field.name] = value;
    }
  }
  return result;
}

function SchemaFields({
  schema,
  form,
  onChange,
}: {
  schema: ParameterSchema[];
  form: ParamForm;
  onChange: (next: ParamForm) => void;
}) {
  return (
    <div className="alerts-form-grid">
      {schema.map((field) => (
        <label key={field.name}>
          {field.label}
          {field.type === 'select' ? (
            <select value={form[field.name] ?? ''} onChange={(event) => onChange({ ...form, [field.name]: event.target.value })}>
              {field.options.map((option) => <option key={option} value={option}>{option}</option>)}
            </select>
          ) : (
            <input
              type="number"
              min={field.minimum ?? undefined}
              max={field.maximum ?? undefined}
              step={field.type === 'integer' ? 1 : 'any'}
              value={form[field.name] ?? ''}
              onChange={(event) => onChange({ ...form, [field.name]: event.target.value })}
              required={!field.optional}
            />
          )}
        </label>
      ))}
    </div>
  );
}

export function Strategies() {
  const [tab, setTab] = useState<'equity' | 'crypto'>('equity');
  const [catalog, setCatalog] = useState<Registration[]>([]);
  const [strategies, setStrategies] = useState<Strategy[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [signals, setSignals] = useState<Signal[]>([]);
  const [signalsLoading, setSignalsLoading] = useState(false);
  const [signalsError, setSignalsError] = useState<string | null>(null);
  const [instruments, setInstruments] = useState<Instrument[]>([]);
  const [instrumentQuery, setInstrumentQuery] = useState('');
  const [instrumentSearchError, setInstrumentSearchError] = useState<string | null>(null);
  const [name, setName] = useState('');
  const [instrumentId, setInstrumentId] = useState('');
  const [createType, setCreateType] = useState('fast_slow_ma');
  const [createForm, setCreateForm] = useState<ParamForm>({});
  const [versionForm, setVersionForm] = useState<ParamForm>({});
  const [dataAvailability, setDataAvailability] = useState<DataAvailability | null>(null);
  const [costForm, setCostForm] = useState<CostForm>({
    brokerage_bps: '3', exchange_charges_bps: '3', slippage_bps: '5',
    stop_atr_multiple: '1.5', target_r_multiple: '3',
  });
  const [backtest, setBacktest] = useState<BacktestResult | null>(null);
  const [backtestingId, setBacktestingId] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [strategiesLoading, setStrategiesLoading] = useState(true);
  const [strategiesError, setStrategiesError] = useState<string | null>(null);

  const selected = strategies.find((item) => item.id === selectedId) ?? null;
  const createDefinition = catalog.find((item) => item.strategy_id === createType) ?? null;
  const selectedType = String(selected?.params.strategy_id ?? 'sma_rsi');
  const selectedDefinition = catalog.find((item) => item.strategy_id === selectedType) ?? null;

  async function loadStrategies() {
    setStrategiesLoading(true);
    setStrategiesError(null);
    try {
      const rows = await apiRequest<Strategy[]>('/strategies');
      setStrategies(rows);
      setSelectedId((current) => current ?? rows[0]?.id ?? null);
    } catch (requestError) {
      setStrategiesError(messageOf(requestError));
      throw requestError;
    } finally {
      setStrategiesLoading(false);
    }
  }

  async function loadSignals(strategyId: string) {
    setSignalsLoading(true);
    setSignalsError(null);
    try {
      setSignals(await apiRequest<Signal[]>(`/strategies/${strategyId}/signals?limit=50`));
    } catch (requestError) {
      setSignalsError(messageOf(requestError));
      throw requestError;
    } finally {
      setSignalsLoading(false);
    }
  }

  useEffect(() => {
    setStrategiesLoading(true);
    Promise.all([
      apiRequest<Registration[]>('/strategies/catalog'),
      apiRequest<Strategy[]>('/strategies'),
    ]).then(([registered, rows]) => {
      setCatalog(registered);
      setStrategies(rows);
      setSelectedId(rows[0]?.id ?? null);
      const defaultType = registered.find((item) => item.strategy_id === 'fast_slow_ma') ?? registered[0];
      if (defaultType) {
        setCreateType(defaultType.strategy_id);
        setCreateForm(initialForm(defaultType.parameters));
      }
    }).catch((err: unknown) => {
      const message = messageOf(err);
      setStrategiesError(message);
      setError(message);
    }).finally(() => setStrategiesLoading(false));
  }, []);

  useEffect(() => {
    if (selectedId) loadSignals(selectedId).catch((err: unknown) => setError(messageOf(err)));
    else { setSignals([]); setSignalsError(null); setSignalsLoading(false); }
  }, [selectedId]);

  useEffect(() => {
    if (selectedDefinition) setVersionForm(initialForm(selectedDefinition.parameters, selected?.params));
  }, [selectedId, selectedType, catalog.length]);

  useEffect(() => {
    const query = instrumentQuery.trim();
    if (query.length < 1) return;
    setInstrumentSearchError(null);
    apiRequest<Instrument[]>(`/market-data/instruments?query=${encodeURIComponent(query)}`)
      .then(setInstruments)
      .catch((requestError: unknown) => {
        setInstruments([]);
        setInstrumentSearchError(messageOf(requestError));
      });
  }, [instrumentQuery]);

  useEffect(() => {
    if (!instrumentId) {
      setDataAvailability(null);
      return;
    }
    const query = new URLSearchParams({
      strategy_id: createType,
      ma_type: createForm.ma_type || 'EMA',
      fast_period: createForm.fast_period || '20',
      slow_period: createForm.slow_period || '50',
    });
    apiRequest<DataAvailability>(`/strategies/data-availability/${instrumentId}?${query.toString()}`)
      .then(setDataAvailability)
      .catch(() => setDataAvailability(null));
  }, [instrumentId, createType, createForm.ma_type, createForm.fast_period, createForm.slow_period]);

  function refreshAfter(action: () => Promise<unknown>, success: string) {
    setError(null);
    setMessage(null);
    action().then(async () => {
      setMessage(success);
      await loadStrategies();
      if (selectedId) await loadSignals(selectedId);
    }).catch((err: unknown) => setError(messageOf(err)));
  }

  function createStrategy(event: FormEvent) {
    event.preventDefault();
    if (!createDefinition) return;
    refreshAfter(async () => {
      const created = await apiRequest<Strategy>('/strategies', {
        method: 'POST',
        body: JSON.stringify({
          name,
          instrument_id: instrumentId,
          params: toParams(createDefinition.parameters, createForm, createType),
        }),
      });
      setSelectedId(created.id);
      setName('');
    }, 'Strategy created as suspended. Activate it to evaluate.');
  }

  function createVersion(event: FormEvent) {
    event.preventDefault();
    if (!selected || !selectedDefinition) return;
    refreshAfter(() => apiRequest(`/strategies/${selected.id}/versions`, {
      method: 'POST',
      body: JSON.stringify({ params: toParams(selectedDefinition.parameters, versionForm, selectedType) }),
    }), 'New version saved.');
  }

  function lifecycle(strategy: Strategy, action: 'activate' | 'suspend' | 'retire') {
    refreshAfter(() => apiRequest(`/strategies/${strategy.id}/${action}`, { method: 'POST', body: '{}' }),
      `Strategy ${action}d.`);
  }

  function evaluate(strategy: Strategy) {
    setError(null);
    setMessage(null);
    apiRequest<{ result: string; data_sufficiency?: DataAvailability }>(`/strategies/${strategy.id}/evaluate`, {
        method: 'POST', body: '{}',
      }).then(async (result) => {
        if (result.result === 'insufficient_history' && result.data_sufficiency) {
          const data = result.data_sufficiency;
          setMessage(`Evaluation: insufficient history for ${data.instrument} ${data.interval} from ${data.source}; ${data.bars_required} bars required, ${data.bars_available} available.`);
        } else {
          setMessage(`Evaluation: ${result.result.replaceAll('_', ' ')}.`);
        }
        await loadStrategies();
        await loadSignals(strategy.id);
      }).catch((err: unknown) => setError(messageOf(err)));
  }

  function runBacktest(strategy: Strategy) {
    setError(null);
    setMessage(null);
    setBacktest(null);
    setBacktestingId(strategy.id);
    apiRequest<BacktestResult>(`/strategies/${strategy.id}/backtest`, {
      method: 'POST',
      body: JSON.stringify(Object.fromEntries(
        Object.entries(costForm).map(([key, value]) => [key, Number(value)]),
      )),
    }).then((result) => {
      setBacktest(result);
      setMessage('Backtest complete. Results describe the past and are not positions, orders, or money at risk.');
    }).catch((err: unknown) => setError(messageOf(err)))
      .finally(() => setBacktestingId(null));
  }

  return (
    <section className="strategies-page">
      <p className="coverage-message">Decision support, not investment advice. Signals never place orders.</p>
      {error && <div className="error-banner" role="alert">{error}</div>}
      {message && <div className="success-banner" role="status">{message}</div>}
      <nav className="instrument-detail-tabs" aria-label="Signals asset class">
        <button type="button" className={tab === 'equity' ? 'active' : ''} aria-pressed={tab === 'equity'} onClick={() => setTab('equity')}>Equity</button>
        <button type="button" className={tab === 'crypto' ? 'active' : ''} aria-pressed={tab === 'crypto'} onClick={() => setTab('crypto')}>Crypto</button>
      </nav>

      {tab === 'equity' ? (
        <>
          <Panel title="Registered equity strategies" description="Availability is based on the stored daily data this workspace currently supports.">
            <div className="strategy-catalog">
              {catalog.filter((item) => item.asset_class === 'equity').map((item) => (
                <article className="data-note" key={item.strategy_id}>
                  <div className="strategy-catalog-copy">
                    <strong>{item.display_name}</strong>
                    <span>{item.availability.status.replaceAll('_', ' ')} · {item.availability.required_data.join(', ')}</span>
                    <span>{item.description}</span>
                    <span>Default minimum history: {Object.entries(item.availability.minimum_history).map(([key, value]) => `${value} ${key} bars`).join(', ')}</span>
                    {item.availability.limitations.map((limitation) => <span key={limitation}>{limitation}</span>)}
                  </div>
                </article>
              ))}
            </div>
          </Panel>

          <Panel title="Backtest assumptions" description="Costs and ATR exits are configurable assumptions, not broker-confirmed values. Every trade uses one analytical unit; no portfolio or live state is created.">
            <div className="alerts-form-grid">
              <label>Brokerage (bps per side)<input type="number" min={0} max={1000} step="any" value={costForm.brokerage_bps} onChange={(event) => setCostForm({ ...costForm, brokerage_bps: event.target.value })} required /></label>
              <label>Exchange charges (bps per side)<input type="number" min={0} max={1000} step="any" value={costForm.exchange_charges_bps} onChange={(event) => setCostForm({ ...costForm, exchange_charges_bps: event.target.value })} required /></label>
              <label>Slippage (bps per side)<input type="number" min={0} max={1000} step="any" value={costForm.slippage_bps} onChange={(event) => setCostForm({ ...costForm, slippage_bps: event.target.value })} required /></label>
              <label>Stop distance (ATR multiples)<input type="number" min={0.01} max={100} step="any" value={costForm.stop_atr_multiple} onChange={(event) => setCostForm({ ...costForm, stop_atr_multiple: event.target.value })} required /></label>
              <label>Target (R multiples)<input type="number" min={0.01} max={100} step="any" value={costForm.target_r_multiple} onChange={(event) => setCostForm({ ...costForm, target_r_multiple: event.target.value })} required /></label>
            </div>
          </Panel>

          <Panel title="New strategy">
            <form className="form-stack" onSubmit={createStrategy}>
              <label>Strategy
                <select value={createType} onChange={(event) => {
                  const nextType = event.target.value;
                  setCreateType(nextType);
                  const definition = catalog.find((item) => item.strategy_id === nextType);
                  if (definition) setCreateForm(initialForm(definition.parameters));
                }}>
                  {catalog.filter((item) => item.asset_class === 'equity').map((item) => (
                    <option key={item.strategy_id} value={item.strategy_id}>{item.display_name}</option>
                  ))}
                </select>
              </label>
              <label>Name<input value={name} onChange={(event) => setName(event.target.value)} maxLength={120} required /></label>
              <label>Find instrument<input value={instrumentQuery} onChange={(event) => setInstrumentQuery(event.target.value)} placeholder="Symbol or name" /></label>
              {instrumentSearchError && <small className="error-message" role="alert">Instrument search failed: {instrumentSearchError}</small>}
              <label>Instrument
                <select value={instrumentId} onChange={(event) => setInstrumentId(event.target.value)} required>
                  <option value="">Select…</option>
                  {instruments.map((item) => <option key={item.id} value={item.id}>{item.symbol} · {item.display_name} ({item.currency})</option>)}
                </select>
              </label>
            {dataAvailability && !dataAvailability.available && (
              <p className="error-message" role="status">
                {dataAvailability.bars_available === 0
                  ? `${dataAvailability.instrument} has no stored ${dataAvailability.interval} bars from ${dataAvailability.source}.`
                  : `${dataAvailability.instrument} has only ${dataAvailability.bars_available} completed ${dataAvailability.interval} bars from ${dataAvailability.source}; ${dataAvailability.bars_required} required.`}
                Strategy creation is available, but evaluation and backtesting will be refused until enough bars are stored.
              </p>
            )}
              {createDefinition && <SchemaFields schema={createDefinition.parameters} form={createForm} onChange={setCreateForm} />}
              <button className="primary-button" type="submit" disabled={!createDefinition}>Create strategy</button>
            </form>
          </Panel>

          <Panel title="Equity strategies">
            {strategiesLoading ? <p className="coverage-message">Loading strategies…</p> : strategiesError ? <div className="error-message" role="alert">Strategies could not be loaded: {strategiesError} <button type="button" onClick={() => {
              setStrategiesLoading(true);
              Promise.all([apiRequest<Registration[]>('/strategies/catalog'), apiRequest<Strategy[]>('/strategies')])
                .then(([registered, rows]) => { setCatalog(registered); setStrategies(rows); setSelectedId(rows[0]?.id ?? null); setStrategiesError(null); })
                .catch((requestError: unknown) => setStrategiesError(messageOf(requestError)))
                .finally(() => setStrategiesLoading(false));
            }}>Retry</button></div> : strategies.length === 0 && <p className="coverage-message">No strategies yet.</p>}
            <div className="coverage-table-wrap">
              <table className="coverage-table strategy-table">
                <thead><tr><th>Name</th><th>Strategy</th><th>Status</th><th>Version</th><th>Actions</th></tr></thead>
                <tbody>{strategies.map((item) => (
                  <tr key={item.id} className={item.id === selectedId ? 'strategy-row selected' : 'strategy-row'} onClick={() => setSelectedId(item.id)}>
                    <td>{item.name}</td>
                    <td>{item.params.strategy_id ?? 'sma_rsi'}</td>
                    <td>{item.status}</td>
                    <td>v{item.current_version}</td>
                    <td onClick={(event) => event.stopPropagation()}><div className="strategy-actions">
                      {item.status !== 'active' && item.status !== 'retired' && <button className="secondary-button" onClick={() => lifecycle(item, 'activate')}>Activate</button>}
                      {item.status === 'active' && <button className="secondary-button" onClick={() => lifecycle(item, 'suspend')}>Suspend</button>}
                      {item.status === 'active' && <button className="secondary-button" onClick={() => evaluate(item)}>Evaluate latest bar</button>}
                      <button className="secondary-button" disabled={backtestingId !== null} onClick={() => runBacktest(item)}>{backtestingId === item.id ? 'Backtesting…' : 'Backtest'}</button>
                      {item.status !== 'retired' && <button className="secondary-button" onClick={() => lifecycle(item, 'retire')}>Retire</button>}
                    </div></td>
                  </tr>
                ))}</tbody>
              </table>
            </div>
          </Panel>

          {backtest && <Panel title={`${backtest.strategy_id} v${backtest.strategy_version} · Backtest results`} description={`${backtest.instrument.symbol} · ${backtest.instrument.currency} · ${backtest.interval} · ${backtest.source} · ${backtest.bar_count} bars · ${backtest.data_window_start.slice(0, 10)} to ${backtest.data_window_end.slice(0, 10)}. ${backtest.disclaimer}`}>
            <div className="metrics-row backtest-metrics">
              {([
                ['Trades closed', 'closed_trades'], ['Net P&L', 'net_pnl'], ['Gross P&L', 'gross_pnl'],
                ['Win rate', 'win_rate'], ['Profit factor', 'profit_factor'], ['Expectancy', 'expectancy'],
                ['Max drawdown', 'max_drawdown'], ['Exposure', 'exposure_pct'], ['Average R', 'average_r'],
                ['Same-bar ambiguity', 'ambiguous_stop_target_trades'], ['Total costs', 'total_costs'],
              ] as const).map(([label, key]) => (
                <article className="metric" key={key}><span>{label}</span><strong>{backtest.metrics[key] ?? '—'}{key === 'win_rate' || key === 'exposure_pct' ? '%' : ''}</strong></article>
              ))}
            </div>
            <h3>Assumptions</h3>
            <dl className="backtest-assumptions">{Object.entries(backtest.assumptions).map(([key, value]) => (
              <div key={key}><dt>{key.replaceAll('_', ' ')}</dt><dd>{String(value)}</dd></div>
            ))}</dl>
            <h3>Closed trades</h3>
            {backtest.trades.length === 0 ? <p className="coverage-message">No closed trades in this data window.</p> : (
              <div className="coverage-table-wrap"><table className="coverage-table">
                <thead><tr><th>Decision close</th><th>Entry time</th><th>Entry fill</th><th>Exit time</th><th>Exit</th><th>Gross</th><th>Costs</th><th>Net</th><th>R</th><th>Ambiguous</th></tr></thead>
                <tbody>{backtest.trades.map((trade, index) => <tr key={`${trade.entry_time}-${index}`}>
                  <td>{trade.decision_time}</td><td>{trade.entry_time}</td><td>{trade.entry_fill}</td>
                  <td>{trade.exit_time}</td><td>{trade.exit_reason} · {trade.exit_fill}</td>
                  <td>{trade.gross_pnl}</td><td>{trade.total_costs}</td><td>{trade.net_pnl}</td>
                  <td>{trade.r_multiple}</td><td>{trade.ambiguous_stop_target_same_bar ? 'Yes' : 'No'}</td>
                </tr>)}</tbody>
              </table></div>
            )}
            {Number(backtest.metrics.unclosed_at_window_end ?? 0) > 0 && <p className="coverage-message">An incomplete historical trade at the end of the window was excluded from closed-trade metrics; it is not a current position.</p>}
          </Panel>}

          {selected && selectedDefinition && <>
            <Panel title={`${selected.name} · version ${selected.current_version}`} description={`Registered strategy: ${selectedDefinition.display_name}.`}>
              {selected.status !== 'retired' && <form className="form-stack" onSubmit={createVersion}>
                <SchemaFields schema={selectedDefinition.parameters} form={versionForm} onChange={setVersionForm} />
                <button className="primary-button" type="submit">Save as new version</button>
              </form>}
            </Panel>
            <Panel title="Signal history">
              {signalsLoading ? <p className="coverage-message">Loading signals…</p> : signalsError ? <div className="error-message" role="alert">Signals could not be loaded: {signalsError} <button type="button" onClick={() => selectedId && void loadSignals(selectedId).catch(() => undefined)}>Retry</button></div> : signals.length === 0 && <p className="coverage-message">No signals yet. Evaluate the latest completed daily bar to create one.</p>}
              <div className="coverage-table-wrap"><table className="coverage-table">
                <thead><tr><th>Bar</th><th>Direction</th><th>Status</th><th>Price</th><th>Rationale</th><th>Expires</th></tr></thead>
                <tbody>{signals.map((signal) => <tr key={signal.id}>
                  <td>{signal.bar_timestamp.slice(0, 10)}</td>
                  <td>{signal.direction}</td>
                  <td>{signal.status}{signal.rejection_reason ? ` (${rejectionLabels[signal.rejection_reason] ?? signal.rejection_reason})` : ''}</td>
                  <td>{signal.trigger_price}</td>
                  <td>{signal.rationale}<details><summary>Evidence pack</summary><pre>{JSON.stringify(signal.evidence, null, 2)}</pre></details></td>
                  <td>{signal.expires_at.slice(0, 16).replace('T', ' ')}</td>
                </tr>)}</tbody>
              </table></div>
            </Panel>
          </>}
        </>
      ) : (
        <>
          <div className="data-note"><div><strong>Delta Exchange research is disabled.</strong><span>No crypto strategies are registered. Public-feed data is shown only when available; unavailable values are not filled or simulated.</span></div></div>
          <DeltaResearchPage />
        </>
      )}
    </section>
  );
}

function messageOf(err: unknown): string {
  if (err instanceof ApiRequestError) return err.message;
  return err instanceof Error ? err.message : 'Request failed.';
}
