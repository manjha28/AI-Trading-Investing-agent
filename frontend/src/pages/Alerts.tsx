import { useEffect, useMemo, useState, type FormEvent } from 'react';
import { ApiRequestError, apiRequest } from '../api';

type Portfolio = { id: string; name: string; base_currency: string };
type Instrument = { id: string; symbol: string; display_name: string; currency: string; venue: string };
type RuleType =
  | 'price_above' | 'price_below' | 'pct_move_nd' | 'trailing_drawdown_pct'
  | 'thesis_target_hit' | 'thesis_stop_hit' | 'unrealized_gain_pct_above'
  | 'position_weight_above' | 'rsi_threshold' | 'ma_cross'
  | 'holding_period_crosses_365d' | 'data_stale';
type Rule = {
  id: string;
  scope: 'instrument' | 'position' | 'portfolio';
  instrument_id: string | null;
  portfolio_id: string | null;
  rule_type: RuleType;
  params: Record<string, string | number>;
  severity: 'info' | 'attention' | 'urgent';
  cooldown_seconds: number;
  enabled: boolean;
};
type Event = {
  id: string;
  rule_id: string;
  instrument_id: string | null;
  fired_at: string;
  evidence: Record<string, unknown>;
  status: 'new' | 'read' | 'dismissed' | 'actioned';
  severity: Rule['severity'];
  rule_type: RuleType;
};

const ruleLabels: Record<RuleType, string> = {
  price_above: 'Price above',
  price_below: 'Price below',
  pct_move_nd: 'N-day price move',
  trailing_drawdown_pct: 'Trailing drawdown',
  thesis_target_hit: 'Position thesis target hit',
  thesis_stop_hit: 'Position thesis stop hit',
  unrealized_gain_pct_above: 'Unrealized gain above',
  position_weight_above: 'Position weight above',
  rsi_threshold: 'RSI threshold',
  ma_cross: 'Moving-average cross',
  holding_period_crosses_365d: 'Holding period reaches 365 days',
  data_stale: 'Market data becomes stale',
};
const ruleTypes = Object.keys(ruleLabels) as RuleType[];
const severityOrder: Rule['severity'][] = ['urgent', 'attention', 'info'];

function initialParams(type: RuleType): Record<string, string> {
  switch (type) {
    case 'price_above':
    case 'price_below': return { price: '' };
    case 'pct_move_nd': return { days: '5', threshold_pct: '5', direction: 'above' };
    case 'trailing_drawdown_pct': return { days: '20', threshold_pct: '10' };
    case 'thesis_target_hit': return { target_index: '0' };
    case 'thesis_stop_hit':
    case 'holding_period_crosses_365d': return {};
    case 'unrealized_gain_pct_above':
    case 'position_weight_above': return { threshold_pct: '10' };
    case 'rsi_threshold': return { period: '14', threshold: '70', direction: 'above' };
    case 'ma_cross': return { fast_period: '20', slow_period: '50', direction: 'up' };
    case 'data_stale': return { max_age_seconds: '3600' };
  }
}

function preview(type: RuleType, params: Record<string, string>): string {
  switch (type) {
    case 'price_above': return `Alert when the latest reliable price reaches ${params.price || '…'} or higher.`;
    case 'price_below': return `Alert when the latest reliable price reaches ${params.price || '…'} or lower.`;
    case 'pct_move_nd': return `Alert when the ${params.days || 'N'}-day move is ${params.direction} ${params.threshold_pct || '…'}%.`;
    case 'trailing_drawdown_pct': return `Alert when price falls ${params.threshold_pct || '…'}% from its ${params.days || 'N'}-day peak.`;
    case 'thesis_target_hit': return `Alert when price reaches target ladder level ${Number(params.target_index || 0) + 1}.`;
    case 'thesis_stop_hit': return 'Alert when price reaches the active thesis stop.';
    case 'unrealized_gain_pct_above': return `Alert when ledger-based unrealized gain reaches ${params.threshold_pct || '…'}%.`;
    case 'position_weight_above': return `Alert when this holding exceeds ${params.threshold_pct || '…'}% of portfolio value.`;
    case 'rsi_threshold': return `Alert when ${params.period || '…'}-period RSI is ${params.direction} ${params.threshold || '…'}.`;
    case 'ma_cross': return `Alert when the ${params.fast_period || '…'}-day average crosses ${params.direction === 'up' ? 'above' : 'below'} the ${params.slow_period || '…'}-day average.`;
    case 'holding_period_crosses_365d': return 'Alert on the date the oldest open acquisition lot reaches 365 days.';
    case 'data_stale': return `Alert when no reliable quote is available within ${params.max_age_seconds || '…'} seconds.`;
  }
}

function paramsFields(type: RuleType): { key: string; label: string; select?: string[]; number?: boolean }[] {
  switch (type) {
    case 'price_above':
    case 'price_below': return [{ key: 'price', label: 'Price', number: true }];
    case 'pct_move_nd': return [
      { key: 'days', label: 'Days', number: true }, { key: 'threshold_pct', label: 'Move (%)', number: true },
      { key: 'direction', label: 'Direction', select: ['above', 'below'] },
    ];
    case 'trailing_drawdown_pct': return [
      { key: 'days', label: 'Lookback days', number: true }, { key: 'threshold_pct', label: 'Drawdown (%)', number: true },
    ];
    case 'thesis_target_hit': return [{ key: 'target_index', label: 'Target ladder level', number: true }];
    case 'thesis_stop_hit':
    case 'holding_period_crosses_365d': return [];
    case 'unrealized_gain_pct_above':
    case 'position_weight_above': return [{ key: 'threshold_pct', label: 'Threshold (%)', number: true }];
    case 'rsi_threshold': return [
      { key: 'period', label: 'Period', number: true }, { key: 'threshold', label: 'RSI level', number: true },
      { key: 'direction', label: 'Direction', select: ['above', 'below'] },
    ];
    case 'ma_cross': return [
      { key: 'fast_period', label: 'Fast period', number: true }, { key: 'slow_period', label: 'Slow period', number: true },
      { key: 'direction', label: 'Cross', select: ['up', 'down'] },
    ];
    case 'data_stale': return [{ key: 'max_age_seconds', label: 'Max age (seconds)', number: true }];
  }
}

function evidenceText(value: unknown): string {
  return JSON.stringify(value, null, 2);
}

export function Alerts({ portfolios, portfolioId }: { portfolios: Portfolio[]; portfolioId: string }) {
  const [rules, setRules] = useState<Rule[]>([]);
  const [events, setEvents] = useState<Event[]>([]);
  const [instruments, setInstruments] = useState<Instrument[]>([]);
  const [instrumentSearch, setInstrumentSearch] = useState('');
  const [instrumentId, setInstrumentId] = useState('');
  const [scope, setScope] = useState<Rule['scope']>('position');
  const [targetPortfolioId, setTargetPortfolioId] = useState(portfolioId);
  const [ruleType, setRuleType] = useState<RuleType>('price_above');
  const [params, setParams] = useState<Record<string, string>>(initialParams('price_above'));
  const [severity, setSeverity] = useState<Rule['severity']>('attention');
  const [cooldown, setCooldown] = useState('3600');
  const [editing, setEditing] = useState<Rule | null>(null);
  const [busy, setBusy] = useState(false);
  const [evaluating, setEvaluating] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [expandedEvidence, setExpandedEvidence] = useState<string | null>(null);
  async function refresh() {
    const [nextRules, nextEvents] = await Promise.all([
      apiRequest<Rule[]>('/alerts/rules'),
      apiRequest<Event[]>('/alerts/events'),
    ]);
    setRules(nextRules);
    setEvents(nextEvents);
  }

  useEffect(() => {
    let active = true;
    Promise.all([apiRequest<Rule[]>('/alerts/rules'), apiRequest<Event[]>('/alerts/events')])
      .then(([nextRules, nextEvents]) => {
        if (active) { setRules(nextRules); setEvents(nextEvents); }
      })
      .catch((requestError: Error) => { if (active) setError(requestError.message); });
    return () => { active = false; };
  }, []);

  useEffect(() => {
    let active = true;
    const query = instrumentSearch.trim();
    if (query.length < 1) { setInstruments([]); return; }
    apiRequest<Instrument[]>(`/market-data/instruments?query=${encodeURIComponent(query)}`)
      .then((found) => { if (active) setInstruments(found); })
      .catch((requestError: Error) => { if (active) setError(requestError.message); });
    return () => { active = false; };
  }, [instrumentSearch]);

  const formPreview = useMemo(() => preview(ruleType, params), [ruleType, params]);

  function startEdit(rule: Rule) {
    setEditing(rule);
    setScope(rule.scope);
    setTargetPortfolioId(rule.portfolio_id ?? portfolioId);
    setRuleType(rule.rule_type);
    setParams(Object.fromEntries(Object.entries(rule.params).map(([key, value]) => [key, String(value)])));
    setSeverity(rule.severity);
    setCooldown(String(rule.cooldown_seconds));
    setInstrumentId(rule.instrument_id ?? '');
    setInstrumentSearch('');
    setError('');
  }

  function startCreate() {
    setEditing(null);
    setScope('position');
    setTargetPortfolioId(portfolioId);
    setRuleType('price_above');
    setParams(initialParams('price_above'));
    setSeverity('attention');
    setCooldown('3600');
    setInstrumentId('');
    setInstrumentSearch('');
    setError('');
  }

  async function saveRule(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError('');
    setNotice('');
    const body = {
      scope,
      portfolio_id: scope === 'instrument' ? null : targetPortfolioId || null,
      instrument_id: scope === 'portfolio' && !instrumentId ? null : instrumentId || null,
      rule_type: ruleType,
      params: Object.fromEntries(Object.entries(params).map(([key, value]) => [
        key, key === 'direction' ? value : Number(value),
      ])),
      severity,
      cooldown_seconds: Number(cooldown),
      enabled: editing?.enabled ?? true,
    };
    try {
      await apiRequest<Rule>(editing ? `/alerts/rules/${editing.id}` : '/alerts/rules', {
        method: editing ? 'PATCH' : 'POST', body: JSON.stringify(body),
      });
      setNotice(editing ? 'Alert rule updated.' : 'Alert rule created.');
      startCreate();
      await refresh();
    } catch (requestError) {
      setError(requestError instanceof ApiRequestError ? requestError.message : 'Could not save the alert rule.');
    } finally {
      setBusy(false);
    }
  }

  async function toggleRule(rule: Rule) {
    try {
      await apiRequest(`/alerts/rules/${rule.id}`, {
        method: 'PATCH', body: JSON.stringify({ enabled: !rule.enabled }),
      });
      await refresh();
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : 'Could not update alert rule.');
    }
  }

  async function deleteRule(rule: Rule) {
    if (!window.confirm('Delete this alert rule and its events?')) return;
    try {
      await apiRequest<void>(`/alerts/rules/${rule.id}`, { method: 'DELETE' });
      await refresh();
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : 'Could not delete alert rule.');
    }
  }

  async function evaluate() {
    setEvaluating(true);
    setError('');
    try {
      const result = await apiRequest<{ fired: Event[]; suppressed: { reason: string }[] }>('/alerts/evaluate', { method: 'POST', body: '{}' });
      setNotice(`${result.fired.length} alert${result.fired.length === 1 ? '' : 's'} fired; ${result.suppressed.length} suppressed for data quality.`);
      await refresh();
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : 'Alert evaluation failed.');
    } finally {
      setEvaluating(false);
    }
  }

  async function updateEvent(event: Event, status: Event['status']) {
    try {
      await apiRequest(`/alerts/events/${event.id}`, { method: 'PATCH', body: JSON.stringify({ status }) });
      await refresh();
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : 'Could not update alert event.');
    }
  }

  return (
    <main className="alerts-page">
      <div className="alerts-intro">
        <div><p className="eyebrow">DETERMINISTIC MONITORING</p><h2>Alerts</h2></div>
        <button className="primary-button" type="button" onClick={() => void evaluate()} disabled={evaluating}>
          {evaluating ? 'Evaluating…' : 'Evaluate now'}
        </button>
      </div>
      <p className="alerts-disclaimer">Decision support, not investment advice. Alerts never place trades. Price-dependent alerts are suppressed when prices are stale, synthetic, unavailable, or in another currency.</p>
      {error && <p className="error-message" role="alert">{error}</p>}
      {notice && <p className="alerts-notice" role="status">{notice}</p>}

      <section className="data-section alerts-rules">
        <div className="section-heading"><div><p className="eyebrow">RULES</p><h3>Alert rules</h3></div><span className="section-count">{rules.length}</span></div>
        {rules.length === 0 ? <p className="table-empty">No alert rules yet.</p> : (
          <div className="alerts-rule-list">
            {rules.map((rule) => (
              <article className="alerts-rule" key={rule.id}>
                <div className="alerts-rule-copy">
                  <div><span className={`severity-badge severity-${rule.severity}`}>{rule.severity}</span><strong>{ruleLabels[rule.rule_type]}</strong></div>
                  <p>{preview(rule.rule_type, Object.fromEntries(Object.entries(rule.params).map(([key, value]) => [key, String(value)])))}</p>
                  <small>{rule.scope}{rule.portfolio_id ? ` · ${portfolios.find((item) => item.id === rule.portfolio_id)?.name ?? 'Portfolio'}` : ''}</small>
                </div>
                <div className="alerts-actions">
                  <label className="alerts-toggle"><input type="checkbox" checked={rule.enabled} onChange={() => void toggleRule(rule)} /><span>{rule.enabled ? 'Enabled' : 'Disabled'}</span></label>
                  <button className="secondary-button" type="button" onClick={() => startEdit(rule)}>Edit</button>
                  <button className="secondary-button" type="button" onClick={() => void deleteRule(rule)}>Delete</button>
                </div>
              </article>
            ))}
          </div>
        )}

        <form className="alerts-form" onSubmit={(event) => void saveRule(event)}>
          <h3>{editing ? 'Edit rule' : 'Create a rule'}</h3>
          <div className="alerts-form-grid">
            <label>Rule type<select value={ruleType} onChange={(event) => {
              const next = event.target.value as RuleType;
              setRuleType(next);
              setParams(initialParams(next));
              if (['thesis_target_hit', 'thesis_stop_hit', 'unrealized_gain_pct_above', 'position_weight_above', 'holding_period_crosses_365d'].includes(next)) setScope('position');
            }}>{ruleTypes.map((type) => <option key={type} value={type}>{ruleLabels[type]}</option>)}</select></label>
            <label>Scope<select value={scope} onChange={(event) => setScope(event.target.value as Rule['scope'])}><option value="position">Position</option><option value="portfolio">Portfolio</option><option value="instrument">Instrument</option></select></label>
            {scope !== 'instrument' && <label>Portfolio<select value={targetPortfolioId} onChange={(event) => setTargetPortfolioId(event.target.value)} required>{portfolios.map((item) => <option key={item.id} value={item.id}>{item.name} · {item.base_currency}</option>)}</select></label>}
            {(scope !== 'portfolio' || ruleType === 'position_weight_above') && (
              <label>Instrument
                <input value={instrumentSearch} onChange={(event) => { setInstrumentSearch(event.target.value); setInstrumentId(''); }} placeholder="Search symbol or name" />
                <select value={instrumentId} onChange={(event) => setInstrumentId(event.target.value)} required={scope !== 'portfolio'}>
                  <option value="">Select instrument</option>
                  {instruments.map((item) => <option key={item.id} value={item.id}>{item.symbol} · {item.display_name} ({item.currency})</option>)}
                </select>
              </label>
            )}
            {paramsFields(ruleType).map((field) => (
              <label key={field.key}>{field.label}
                {field.select
                  ? <select value={params[field.key] ?? field.select[0]} onChange={(event) => setParams({ ...params, [field.key]: event.target.value })}>{field.select.map((option) => <option key={option} value={option}>{option}</option>)}</select>
                  : <input type={field.number ? 'number' : 'text'} step={field.key.includes('period') || field.key === 'days' || field.key === 'target_index' || field.key === 'max_age_seconds' ? 1 : 'any'} value={params[field.key] ?? ''} required onChange={(event) => setParams({ ...params, [field.key]: event.target.value })} />}
              </label>
            ))}
            <label>Severity<select value={severity} onChange={(event) => setSeverity(event.target.value as Rule['severity'])}><option value="info">Info</option><option value="attention">Attention</option><option value="urgent">Urgent</option></select></label>
            <label>Cooldown (seconds)<input type="number" min={0} max={31536000} value={cooldown} onChange={(event) => setCooldown(event.target.value)} required /></label>
          </div>
          <p className="alerts-preview"><strong>Trigger preview:</strong> {formPreview}</p>
          <div className="alerts-actions">
            <button className="primary-button" type="submit" disabled={busy || !targetPortfolioId && scope !== 'instrument'}>{busy ? 'Saving…' : editing ? 'Save rule' : 'Create rule'}</button>
            {editing && <button className="secondary-button" type="button" onClick={startCreate}>Cancel edit</button>}
          </div>
        </form>
      </section>

      <section className="data-section alerts-inbox">
        <div className="section-heading"><div><p className="eyebrow">INBOX</p><h3>Alert events</h3></div><span className="section-count">{events.filter((event) => event.status === 'new').length} new</span></div>
        {events.length === 0 ? <p className="table-empty">No alert events. Use Evaluate now to check enabled rules against stored data.</p> : severityOrder.map((level) => {
          const grouped = events.filter((event) => event.severity === level);
          if (!grouped.length) return null;
          return <section className="alerts-event-group" key={level}>
            <h4><span className={`severity-badge severity-${level}`}>{level}</span> {grouped.length} event{grouped.length === 1 ? '' : 's'}</h4>
            {grouped.map((event) => (
              <article className="alerts-event" key={event.id}>
                <header><div><strong>{ruleLabels[event.rule_type]}</strong><span>{new Date(event.fired_at).toLocaleString()}</span></div><span className={`event-status event-${event.status}`}>{event.status}</span></header>
                <button className="text-button" type="button" aria-expanded={expandedEvidence === event.id} onClick={() => setExpandedEvidence(expandedEvidence === event.id ? null : event.id)}>{expandedEvidence === event.id ? 'Hide evidence' : 'Show evidence'}</button>
                {expandedEvidence === event.id && <pre className="alerts-evidence">{evidenceText(event.evidence)}</pre>}
                <div className="alerts-actions">{(['read', 'dismissed', 'actioned'] as const).map((status) => <button className="secondary-button" type="button" key={status} disabled={event.status === status} onClick={() => void updateEvent(event, status)}>{status === 'dismissed' ? 'Dismiss' : status === 'actioned' ? 'Mark actioned' : 'Mark read'}</button>)}</div>
              </article>
            ))}
          </section>;
        })}
      </section>
    </main>
  );
}
