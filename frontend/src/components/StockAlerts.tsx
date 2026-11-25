import { useCallback, useEffect, useState } from 'react';
import { apiRequest, ApiRequestError } from '../api';
import { formatIST } from '../lib/formatIST';

export type AlertRule = { id: string; scope: string; instrument_id: string | null; rule_type: string; params: Record<string, unknown>; severity: string; enabled: boolean; last_status: string | null; last_reason: string | null };
export type AlertEvent = { id: string; rule_id: string; instrument_id: string | null; fired_at: string; evidence: Record<string, unknown>; status: string; severity: string; rule_type: string };

type Field = { key: string; label: string; kind: 'decimal' | 'int' | 'choice'; default: string; choices?: string[] };
export const STOCK_RULES: Record<string, { label: string; fields: Field[] }> = {
  price_above: { label: 'Price rises above', fields: [{ key: 'price', label: 'Price (₹)', kind: 'decimal', default: '' }] },
  price_below: { label: 'Price falls below', fields: [{ key: 'price', label: 'Price (₹)', kind: 'decimal', default: '' }] },
  pct_move_nd: { label: 'Moves by a % over N days', fields: [
    { key: 'days', label: 'Days', kind: 'int', default: '5' }, { key: 'threshold_pct', label: 'Move (%)', kind: 'decimal', default: '5' },
    { key: 'direction', label: 'Direction', kind: 'choice', default: 'above', choices: ['above', 'below'] }] },
  rsi_threshold: { label: 'RSI crosses a level', fields: [
    { key: 'period', label: 'Period', kind: 'int', default: '14' }, { key: 'threshold', label: 'Level', kind: 'decimal', default: '70' },
    { key: 'direction', label: 'Direction', kind: 'choice', default: 'above', choices: ['above', 'below'] }] },
  ma_cross: { label: 'Moving averages cross', fields: [
    { key: 'fast_period', label: 'Fast', kind: 'int', default: '9' }, { key: 'slow_period', label: 'Slow', kind: 'int', default: '21' },
    { key: 'direction', label: 'Direction', kind: 'choice', default: 'up', choices: ['up', 'down'] }] },
};
export const ruleLabel = (type: string) => STOCK_RULES[type]?.label ?? type.replace(/_/g, ' ');
export const describeParams = (params: Record<string, unknown>) => Object.entries(params).map(([k, v]) => `${k.replace(/_/g, ' ')} ${String(v)}`).join(' · ');
const messageOf = (e: unknown) => (e instanceof ApiRequestError || e instanceof Error ? e.message : 'Request failed.');
const when = (iso: string) => formatIST(iso);

/** Alerts for ONE stock: its rules, a quick form to add one, and what has fired. Everything is owner-scoped on the server. */
export function StockAlerts({ instrumentId, symbol, lastPrice, onChanged }: { instrumentId: string; symbol: string; lastPrice: string | null; onChanged?: () => void }) {
  const [rules, setRules] = useState<AlertRule[]>([]);
  const [events, setEvents] = useState<AlertEvent[]>([]);
  const [type, setType] = useState('price_above');
  const [values, setValues] = useState<Record<string, string>>({});
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const [r, e] = await Promise.all([apiRequest<AlertRule[]>('/alerts/rules'), apiRequest<AlertEvent[]>('/alerts/events?limit=100')]);
      setRules(r.filter((x) => x.instrument_id === instrumentId));
      setEvents(e.filter((x) => x.instrument_id === instrumentId).slice(0, 8));
    } catch (err) { setError(messageOf(err)); }
  }, [instrumentId]);
  useEffect(() => { setError(null); setNotice(null); void load(); }, [load]);
  useEffect(() => {
    const defaults = Object.fromEntries(STOCK_RULES[type].fields.map((f) => [f.key, f.default]));
    if (type === 'price_above' && lastPrice) defaults.price = (Number(lastPrice) * 1.03).toFixed(2);
    if (type === 'price_below' && lastPrice) defaults.price = (Number(lastPrice) * 0.97).toFixed(2);
    setValues(defaults);
  }, [type, lastPrice, instrumentId]);

  async function create() {
    setBusy(true); setError(null); setNotice(null);
    try {
      const params: Record<string, string | number> = {};
      for (const f of STOCK_RULES[type].fields) params[f.key] = f.kind === 'int' ? Number(values[f.key]) : (values[f.key] ?? '').trim();
      await apiRequest('/alerts/rules', { method: 'POST', body: JSON.stringify({ scope: 'instrument', instrument_id: instrumentId, rule_type: type, params, severity: 'attention', cooldown_seconds: 3600, enabled: true }) });
      setNotice(`Alert added for ${symbol}. It is checked automatically; you will see it on the bell when it fires.`);
      await load(); onChanged?.();
    } catch (err) { setError(messageOf(err)); } finally { setBusy(false); }
  }
  async function toggle(rule: AlertRule) {
    try { await apiRequest(`/alerts/rules/${rule.id}`, { method: 'PATCH', body: JSON.stringify({ enabled: !rule.enabled }) }); await load(); onChanged?.(); } catch (err) { setError(messageOf(err)); }
  }
  async function remove(rule: AlertRule) {
    try { await apiRequest<void>(`/alerts/rules/${rule.id}`, { method: 'DELETE' }); await load(); onChanged?.(); } catch (err) { setError(messageOf(err)); }
  }
  async function checkNow() {
    setBusy(true); setError(null);
    try {
      const r = await apiRequest<{ fired: unknown[] }>('/alerts/evaluate', { method: 'POST' });
      setNotice(r.fired.length ? `${r.fired.length} alert(s) fired.` : 'Checked now: nothing has triggered.');
      await load(); onChanged?.();
    } catch (err) { setError(messageOf(err)); } finally { setBusy(false); }
  }

  return (
    <div className="stock-panel" aria-label={`Alerts for ${symbol}`}>
      <div className="stock-add">
        <label>Alert me when<select aria-label="Alert type" value={type} onChange={(e) => setType(e.target.value)}>{Object.entries(STOCK_RULES).map(([k, v]) => <option key={k} value={k}>{v.label}</option>)}</select></label>
        {STOCK_RULES[type].fields.map((f) => f.kind === 'choice'
          ? <label key={f.key}>{f.label}<select aria-label={f.label} value={values[f.key] ?? f.default} onChange={(e) => setValues({ ...values, [f.key]: e.target.value })}>{f.choices?.map((c) => <option key={c} value={c}>{c}</option>)}</select></label>
          : <label key={f.key}>{f.label}<input aria-label={f.label} value={values[f.key] ?? ''} inputMode="decimal" onChange={(e) => setValues({ ...values, [f.key]: e.target.value })} /></label>)}
        <button type="button" className="secondary-button" disabled={busy} onClick={() => void create()}>＋ Add alert</button>
      </div>
      {error && <p className="alerts-notice" role="alert">{error}</p>}
      {notice && <p className="crypto-sub" role="status">{notice}</p>}
      <h4 className="stock-h">Alerts on {symbol}</h4>
      {rules.length === 0 ? <p className="crypto-sub">None yet.</p> : (
        <ul className="stock-list">{rules.map((r) => (
          <li key={r.id}>
            <span><strong>{ruleLabel(r.rule_type)}</strong> <small>{describeParams(r.params)}</small>{!r.enabled && <em> · paused</em>}
              {r.last_reason && <small className="stock-reason" title={r.last_reason}> · {r.last_status ?? ''}</small>}</span>
            <span><button type="button" className="text-button" onClick={() => void toggle(r)}>{r.enabled ? 'Pause' : 'Resume'}</button> <button type="button" className="text-button" aria-label={`Delete ${ruleLabel(r.rule_type)} alert`} onClick={() => void remove(r)}>Delete</button></span>
          </li>))}</ul>
      )}
      <div className="stock-row"><h4 className="stock-h">Recently fired</h4><button type="button" className="text-button" disabled={busy} onClick={() => void checkNow()}>Check now</button></div>
      {events.length === 0 ? <p className="crypto-sub">Nothing has fired for this stock.</p> : (
        <ul className="stock-list">{events.map((e) => <li key={e.id}><span><strong>{ruleLabel(e.rule_type)}</strong> <small>{when(e.fired_at)}</small></span><span className={`severity-badge severity-${e.severity}`}>{e.severity}</span></li>)}</ul>
      )}
    </div>
  );
}
