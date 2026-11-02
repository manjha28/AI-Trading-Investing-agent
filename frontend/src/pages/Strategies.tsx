import { useEffect, useState, type FormEvent } from 'react';
import { ApiRequestError, apiRequest } from '../api';

type Instrument = { id: string; symbol: string; display_name: string; currency: string };
type Params = {
  fast_period: number;
  slow_period: number;
  rsi_max: string | null;
  max_bar_age_days: number;
  expiry_hours: number;
};
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
  expires_at: string;
};
type ParamForm = { fast: string; slow: string; rsi: string; maxAge: string; expiry: string };

const defaultForm: ParamForm = { fast: '20', slow: '50', rsi: '', maxAge: '5', expiry: '48' };

const rejectionLabels: Record<string, string> = {
  stale_bar: 'Latest bar is too old',
  synthetic_price: 'Price is synthetic',
  rsi_filter: 'RSI filter not met',
};

function toParams(form: ParamForm): Params {
  return {
    fast_period: Number(form.fast),
    slow_period: Number(form.slow),
    rsi_max: form.rsi.trim() === '' ? null : form.rsi.trim(),
    max_bar_age_days: Number(form.maxAge),
    expiry_hours: Number(form.expiry),
  };
}

function ParamFields({ form, onChange }: { form: ParamForm; onChange: (next: ParamForm) => void }) {
  const set = (key: keyof ParamForm) => (event: React.ChangeEvent<HTMLInputElement>) =>
    onChange({ ...form, [key]: event.target.value });
  return (
    <div className="form-grid">
      <label>Fast SMA (days)<input type="number" min={2} max={100} value={form.fast} onChange={set('fast')} required /></label>
      <label>Slow SMA (days)<input type="number" min={3} max={250} value={form.slow} onChange={set('slow')} required /></label>
      <label>Max RSI(14) for entry (optional)<input type="number" min={0} max={100} value={form.rsi} onChange={set('rsi')} /></label>
      <label>Max bar age (days)<input type="number" min={1} max={10} value={form.maxAge} onChange={set('maxAge')} required /></label>
      <label>Signal expiry (hours)<input type="number" min={1} max={720} value={form.expiry} onChange={set('expiry')} required /></label>
    </div>
  );
}

export function Strategies() {
  const [strategies, setStrategies] = useState<Strategy[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [signals, setSignals] = useState<Signal[]>([]);
  const [instruments, setInstruments] = useState<Instrument[]>([]);
  const [instrumentQuery, setInstrumentQuery] = useState('');
  const [name, setName] = useState('');
  const [instrumentId, setInstrumentId] = useState('');
  const [createForm, setCreateForm] = useState<ParamForm>(defaultForm);
  const [versionForm, setVersionForm] = useState<ParamForm>(defaultForm);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const selected = strategies.find((item) => item.id === selectedId) ?? null;

  async function loadStrategies() {
    const rows = await apiRequest<Strategy[]>('/strategies');
    setStrategies(rows);
    setSelectedId((current) => current ?? rows[0]?.id ?? null);
  }

  async function loadSignals(strategyId: string) {
    setSignals(await apiRequest<Signal[]>(`/strategies/${strategyId}/signals?limit=50`));
  }

  useEffect(() => {
    loadStrategies().catch((err: unknown) => setError(messageOf(err)));
  }, []);

  useEffect(() => {
    if (selectedId) {
      loadSignals(selectedId).catch((err: unknown) => setError(messageOf(err)));
    } else {
      setSignals([]);
    }
  }, [selectedId]);

  useEffect(() => {
    const query = instrumentQuery.trim();
    if (query.length < 1) return;
    apiRequest<Instrument[]>(`/market-data/instruments?query=${encodeURIComponent(query)}`)
      .then(setInstruments)
      .catch(() => setInstruments([]));
  }, [instrumentQuery]);

  function run(action: () => Promise<unknown>, success: string) {
    setError(null);
    setMessage(null);
    action()
      .then(async () => {
        setMessage(success);
        await loadStrategies();
        if (selectedId) await loadSignals(selectedId);
      })
      .catch((err: unknown) => setError(messageOf(err)));
  }

  function createStrategy(event: FormEvent) {
    event.preventDefault();
    run(async () => {
      const created = await apiRequest<Strategy>('/strategies', {
        method: 'POST',
        body: JSON.stringify({ name, instrument_id: instrumentId, params: toParams(createForm) }),
      });
      setSelectedId(created.id);
      setName('');
    }, 'Strategy created as suspended. Activate it to evaluate.');
  }

  function createVersion(event: FormEvent) {
    event.preventDefault();
    if (!selected) return;
    run(() => apiRequest(`/strategies/${selected.id}/versions`, {
      method: 'POST',
      body: JSON.stringify({ params: toParams(versionForm) }),
    }), 'New version saved.');
  }

  function lifecycle(strategy: Strategy, action: 'activate' | 'suspend' | 'retire') {
    run(() => apiRequest(`/strategies/${strategy.id}/${action}`, { method: 'POST', body: '{}' }),
      `Strategy ${action}d.`);
  }

  function evaluate(strategy: Strategy) {
    run(async () => {
      const result = await apiRequest<{ result: string }>(`/strategies/${strategy.id}/evaluate`, {
        method: 'POST', body: '{}',
      });
      setMessage(`Evaluation: ${result.result.replace('_', ' ')}.`);
    }, 'Evaluation complete.');
  }

  return (
    <section className="page">
      <p className="muted">Decision support, not investment advice. Strategies never place orders.</p>
      {error && <div className="notice error">{error}</div>}
      {message && <div className="notice">{message}</div>}

      <div className="card">
        <h3>New strategy</h3>
        <form onSubmit={createStrategy}>
          <label>Name<input value={name} onChange={(e) => setName(e.target.value)} maxLength={120} required /></label>
          <label>Find instrument<input value={instrumentQuery} onChange={(e) => setInstrumentQuery(e.target.value)} placeholder="Symbol or name" /></label>
          <label>Instrument
            <select value={instrumentId} onChange={(e) => setInstrumentId(e.target.value)} required>
              <option value="">Select…</option>
              {instruments.map((item) => <option key={item.id} value={item.id}>{item.symbol} · {item.display_name} ({item.currency})</option>)}
            </select>
          </label>
          <ParamFields form={createForm} onChange={setCreateForm} />
          <button type="submit">Create strategy</button>
        </form>
      </div>

      <div className="card">
        <h3>Strategies</h3>
        {strategies.length === 0 && <p className="muted">No strategies yet.</p>}
        <table>
          <thead><tr><th>Name</th><th>Status</th><th>Version</th><th>Actions</th></tr></thead>
          <tbody>
            {strategies.map((item) => (
              <tr key={item.id} className={item.id === selectedId ? 'selected' : undefined} onClick={() => setSelectedId(item.id)}>
                <td>{item.name}</td>
                <td>{item.status}</td>
                <td>v{item.current_version}</td>
                <td onClick={(e) => e.stopPropagation()}>
                  {item.status !== 'active' && item.status !== 'retired' && <button onClick={() => lifecycle(item, 'activate')}>Activate</button>}
                  {item.status === 'active' && <button onClick={() => lifecycle(item, 'suspend')}>Suspend</button>}
                  {item.status === 'active' && <button onClick={() => evaluate(item)}>Evaluate latest bar</button>}
                  {item.status !== 'retired' && <button onClick={() => lifecycle(item, 'retire')}>Retire</button>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {selected && (
        <>
          <div className="card">
            <h3>{selected.name} · version {selected.current_version}</h3>
            <p className="muted">Current parameters: fast {selected.params.fast_period}, slow {selected.params.slow_period}, RSI max {selected.params.rsi_max ?? 'none'}, max bar age {selected.params.max_bar_age_days}d, expiry {selected.params.expiry_hours}h.</p>
            {selected.status !== 'retired' && (
              <form onSubmit={createVersion}>
                <ParamFields form={versionForm} onChange={setVersionForm} />
                <button type="submit">Save as new version</button>
              </form>
            )}
          </div>

          <div className="card">
            <h3>Signal history</h3>
            {signals.length === 0 && <p className="muted">No signals yet. Evaluate the latest completed daily bar to create one.</p>}
            <table>
              <thead><tr><th>Bar</th><th>Direction</th><th>Status</th><th>Price</th><th>Rationale</th><th>Expires</th></tr></thead>
              <tbody>
                {signals.map((signal) => (
                  <tr key={signal.id}>
                    <td>{signal.bar_timestamp.slice(0, 10)}</td>
                    <td>{signal.direction}</td>
                    <td>{signal.status}{signal.rejection_reason ? ` (${rejectionLabels[signal.rejection_reason] ?? signal.rejection_reason})` : ''}</td>
                    <td>{signal.trigger_price}</td>
                    <td>{signal.rationale}</td>
                    <td>{signal.expires_at.slice(0, 16).replace('T', ' ')}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </section>
  );
}

function messageOf(err: unknown): string {
  if (err instanceof ApiRequestError) return err.message;
  return err instanceof Error ? err.message : 'Request failed.';
}
