import { useCallback, useEffect, useState, type FormEvent } from 'react';
import { apiRequest } from '../api';
import { ExecutionStateChip } from '../components/ExecutionStateChip';
import { useExecutionState } from '../hooks/useExecutionState';
import { messageOf } from './cryptoApi';

type ExecutionStatus = {
  mode: { configured: string; effective: string; live_blocked: boolean; reason: string | null };
  capabilities: { order_transmission: boolean; live_adapter: boolean; execution_credentials: string; simulated_fills: string };
  limits: { currency: string; max_order_notional: string; max_total_exposure: string; max_price_age_seconds: number };
  audit_events: number;
  disclaimer: string;
};

type Check = { name: string; passed: boolean; detail: string };
type DryRun = {
  mode: string;
  client_order_id: string;
  decision: { approved: boolean; notional: string | null; currency: string; reasons: string[]; checks: Check[] };
  order_transmission: string;
  note: string;
};

type AuditRow = {
  id: string;
  created_at: string;
  event_type: 'intent' | 'decision' | 'fill';
  mode: string;
  client_order_id: string;
  symbol: string;
  hypothetical: boolean;
  payload: Record<string, unknown>;
};

const checkLabels: Record<string, string> = {
  price_available: 'Mark price available',
  price_not_synthetic: 'Price is real market data',
  price_fresh: 'Price is fresh',
  currency_compatible: 'Currency matches (no FX)',
  product_tradable: 'Contract is live',
  order_not_expired: 'Order validity open',
  notional_computable: 'Notional computable',
  order_notional_within_cap: 'Order size within cap',
  exposure_verifiable: 'Current exposure verifiable',
  total_exposure_within_cap: 'Total exposure within cap',
};

/** What an audit row means. Only guard-rail decisions carry approved/allowed; a switch change (arm, disarm, per-test Live) is a record of the change, not a blocked order. */
function auditOutcome(row: { event_type: string; payload: Record<string, unknown>; mode: string }): string {
  const p = row.payload;
  if (row.event_type === 'broker_reply') {                       // what the broker answered: a refusal has no order id and never appears in the broker's own order list
    const reason = typeof p.reason === 'string' && p.reason ? `: ${p.reason.replace(/_/g, ' ')}` : '';
    return typeof p.status === 'string' ? `${p.status}${reason}` : '—';
  }
  if (row.event_type !== 'decision') return '—';
  if (typeof p.approved === 'boolean') return p.approved ? 'would pass' : 'blocked';
  if (typeof p.allowed === 'boolean') return p.allowed ? 'allowed' : 'blocked';
  if (typeof p.event === 'string') return p.event.includes('execute_live') ? `Live switch ${p.enabled ? 'turned on' : 'turned off'}` : p.event;
  return '—';
}

/** Execution readiness, risk-check sandbox and audit trail. Orders go out only when armed. */
export function CryptoExecution() {
  const states = useExecutionState();
  const delta = states?.delta;
  const [status, setStatus] = useState<ExecutionStatus | null>(null);
  const [statusError, setStatusError] = useState<string | null>(null);
  const [audit, setAudit] = useState<AuditRow[]>([]);
  const [symbol, setSymbol] = useState('BTCUSD');
  const [side, setSide] = useState<'buy' | 'sell'>('buy');
  const [orderType, setOrderType] = useState<'market' | 'limit' | 'stop_market'>('limit');
  const [quantity, setQuantity] = useState('1');
  const [limitPrice, setLimitPrice] = useState('');
  const [stopPrice, setStopPrice] = useState('');
  const [reduceOnly, setReduceOnly] = useState(false);
  const [strategyId, setStrategyId] = useState('manual');
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<DryRun | null>(null);
  const [formError, setFormError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    const [s, a] = await Promise.allSettled([
      apiRequest<ExecutionStatus>('/execution/status'),
      apiRequest<AuditRow[]>('/execution/audit?limit=20'),
    ]);
    if (s.status === 'fulfilled') { setStatus(s.value); setStatusError(null); } else setStatusError(messageOf(s.reason));
    if (a.status === 'fulfilled') setAudit(a.value);
  }, []);

  useEffect(() => { void refresh(); }, [refresh]);

  async function runDryRun(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setFormError(null);
    try {
      const intent: Record<string, unknown> = {
        symbol: symbol.trim().toUpperCase(),
        side,
        quantity: quantity.trim(),
        order_type: orderType,
        reduce_only: reduceOnly,
        strategy_id: strategyId.trim() || 'manual',
        strategy_version: '1',
        signal_id: `manual-${new Date().toISOString()}`,
      };
      if (orderType === 'limit') intent.limit_price = limitPrice.trim();
      if (orderType === 'stop_market') intent.stop_price = stopPrice.trim();
      setResult(await apiRequest<DryRun>('/execution/dry-run', { method: 'POST', body: JSON.stringify({ intent }) }));
      await refresh();
    } catch (error) {
      setResult(null);
      setFormError(messageOf(error));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="crypto-page">
      <section className="data-section crypto-readiness" aria-label="Execution readiness">
        <div className="section-heading"><div><p className="eyebrow">EXECUTION READINESS</p><h3>Delta orders: {delta ? delta.effective.state.replace(/_/g, ' ') : 'checking…'}</h3></div></div>
        {statusError ? <p className="error-message" role="alert">{statusError}</p> : !status ? <p className="table-empty">Loading…</p> : (
          <div className="crypto-readiness-grid">
            <div><span>Execution state</span><strong>{delta ? <ExecutionStateChip venue={delta} /> : '—'}</strong><small>{delta ? (delta.effective.reasons.join('; ') || 'Orders can be sent now') : 'The server has not reported it yet'}</small></div>
            <div><span>Mode</span><strong>{status.mode.effective}</strong><small>{status.mode.live_blocked ? status.mode.reason : 'Research mode does not decide live orders: the execution state does'}</small></div>
            <div><span>Max order notional</span><strong>{status.limits.max_order_notional} {status.limits.currency}</strong><small>Fails closed on unknown inputs</small></div>
            <div><span>Max total exposure</span><strong>{status.limits.max_total_exposure} {status.limits.currency}</strong><small>Price must be under {status.limits.max_price_age_seconds}s old</small></div>
            <div><span>Credentials</span><strong>Backend secrets</strong><small>{status.capabilities.execution_credentials}</small></div>
            <div><span>Audit events</span><strong>{status.audit_events}</strong><small>Append-only, owner-scoped</small></div>
          </div>
        )}
      </section>

      <section className="data-section" aria-label="Risk check">
        <div className="section-heading"><div><p className="eyebrow">RISK-CHECK SANDBOX</p><h3>Would this order pass the guard-rails?</h3></div></div>
        <p className="crypto-sub">Runs the fail-closed checks against stored real Delta data and records the result in the audit trail. Nothing is sent to the exchange.</p>
        <form className="crypto-form" onSubmit={(event) => void runDryRun(event)}>
          <label>Contract<input value={symbol} onChange={(event) => setSymbol(event.target.value)} placeholder="BTCUSD" required /></label>
          <label>Side<select value={side} onChange={(event) => setSide(event.target.value as 'buy' | 'sell')}><option value="buy">Buy</option><option value="sell">Sell</option></select></label>
          <label>Order type<select value={orderType} onChange={(event) => setOrderType(event.target.value as typeof orderType)}><option value="limit">Limit</option><option value="market">Market</option><option value="stop_market">Stop market</option></select></label>
          <label>Contracts<input value={quantity} onChange={(event) => setQuantity(event.target.value)} inputMode="decimal" required /></label>
          {orderType === 'limit' && <label>Limit price (USD)<input value={limitPrice} onChange={(event) => setLimitPrice(event.target.value)} inputMode="decimal" required /></label>}
          {orderType === 'stop_market' && <label>Stop price (USD)<input value={stopPrice} onChange={(event) => setStopPrice(event.target.value)} inputMode="decimal" required /></label>}
          <label>Strategy<input value={strategyId} onChange={(event) => setStrategyId(event.target.value)} /></label>
          <label className="crypto-check"><input type="checkbox" checked={reduceOnly} onChange={(event) => setReduceOnly(event.target.checked)} /> Reduce only</label>
          <button className="primary-button" type="submit" disabled={busy}>{busy ? 'Checking…' : 'Run risk check'}</button>
        </form>
        {formError && <p className="error-message" role="alert">{formError}</p>}
        {result && (
          <div className="crypto-result" role="status">
            <p className={result.decision.approved ? 'crypto-verdict crypto-verdict-pass' : 'crypto-verdict crypto-verdict-block'}>
              {result.decision.approved ? 'Would pass the risk checks' : 'Blocked by the risk checks'}
              {result.decision.notional && <span> · notional {result.decision.notional} {result.decision.currency}</span>}
            </p>
            <ul className="crypto-checks">
              {result.decision.checks.map((check) => (
                <li key={check.name} className={check.passed ? 'crypto-check-pass' : 'crypto-check-fail'}>
                  <strong>{check.passed ? '✓' : '✕'} {checkLabels[check.name] ?? check.name}</strong><span>{check.detail}</span>
                </li>
              ))}
            </ul>
            <small className="crypto-sub">Client order ID {result.client_order_id} · {result.note}</small>
          </div>
        )}
      </section>

      <section className="data-section" aria-label="Audit trail">
        <div className="section-heading"><div><p className="eyebrow">AUDIT TRAIL</p><h3>Recent intents and decisions</h3></div><span className="section-count">{audit.length} shown</span></div>
        {audit.length === 0 ? <p className="table-empty">No risk checks recorded yet.</p> : (
          <div className="table-scroll"><table>
            <thead><tr><th>Time</th><th>Event</th><th>Contract</th><th>Mode</th><th>Order ID</th><th>Result</th></tr></thead>
            <tbody>{audit.map((row) => (
              <tr key={row.id}>
                <td>{new Date(row.created_at).toLocaleString()}</td>
                <td>{row.event_type}{row.hypothetical ? ' · hypothetical' : ''}</td>
                <td>{row.symbol}</td>
                <td>{row.mode}</td>
                <td><code>{row.client_order_id}</code></td>
                <td>{auditOutcome(row)}</td>
              </tr>
            ))}</tbody>
          </table></div>
        )}
      </section>

    </div>
  );
}
