import { useCallback, useEffect, useState, type FormEvent } from 'react';
import { apiRequest } from '../api';
import { messageOf } from './cryptoApi';
import { ExecutionStateChip } from '../components/ExecutionStateChip';
import { ArmConfirm } from '../components/ArmConfirm';
import { refreshExecutionState } from '../hooks/useExecutionState';
import { R3PaperCard } from './R3PaperCard';
import { CryptoScanner } from './CryptoScanner';
import { accountLine, testModeLine, todayLine, type VenueState } from '../lib/executionState';

type Summary = { closed_trades: number; wins: number; losses: number; win_rate: number | null; gross_pnl: string; fees: string; slippage: string; net_pnl: string; avg_r: string | null };
type Trade = { side: string; entry_time: string; exit_time: string | null; entry_fill: string; exit_fill: string; exit_reason: string; net_pnl: string; r_multiple: string | null };
type OpenPos = { side: string; entry_time: string; entry_fill: string; stop_initial: string; target: string; mark: string };
type LiveStatus = { deployment_enabled: boolean; credentials_present: boolean; armed: boolean; can_arm: boolean; confirmation_phrase: string; warning: string; execution: VenueState; limits: { max_contracts: number; leverage: string; allowed_strategies: string[]; max_daily_loss_usd: string; max_trades_per_day: number; max_open_positions: number; max_notional_usd: string } };
type LiveOrder = { id: string; created_at: string; symbol: string; side: string; contracts: number; leverage: number; status: string; block_reason: string | null; delta_order_id: string | null };
type Blocked = { id: string; created_at: string; symbol: string; side: string; contracts: number; block_reason: string | null };
type Forward = {
  id: string; name: string; symbol: string; resolution: string; status: string; contracts: number; start_time: string; last_run_at: string | null; last_error: string | null;
  execute_live?: boolean; label: string; summary: Summary | null; open_positions: OpenPos[]; trades?: Trade[]; params: { ema: number; rr: string; direction: number };
};

const num = (v: string | null | undefined, digits = 2) => (v === null || v === undefined || v === '' || Number.isNaN(Number(v)) ? '—' : Number(v).toLocaleString('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits }));
const when = (iso: string | null) => (iso ? new Date(iso).toLocaleString('en-IN', { timeZone: 'Asia/Kolkata', hour12: false }) : '—');
const DIRECTIONS: Record<string, string> = { '-1': 'Sell only', '0': 'Both directions', '1': 'Buy only' };

/** Paper forward test of the 5 EMA alert-candle strategy on a Delta perpetual. Hypothetical; no order is placed anywhere. */
export function CryptoForward({ symbol, resolution }: { symbol: string; resolution: string }) {
  const [tests, setTests] = useState<Forward[]>([]);
  const [open, setOpen] = useState<Forward | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [rr, setRr] = useState('3');
  const [direction, setDirection] = useState('-1');
  const [contracts, setContracts] = useState('1');
  const [live, setLive] = useState<LiveStatus | null>(null);
  const [orders, setOrders] = useState<LiveOrder[]>([]);
  const [blocked, setBlocked] = useState<Blocked[]>([]);
  const [confirming, setConfirming] = useState(false);
  const [phrase, setPhrase] = useState('');
  const [tf, setTf] = useState(['5m', '15m', '1h'].includes(resolution) ? resolution : '15m');

  const load = useCallback(async () => {
    try { setTests((await apiRequest<{ tests: Forward[] }>('/crypto-delta/forward-tests')).tests); } catch (e) { setError(messageOf(e)); }
  }, []);
  const loadLive = useCallback(async () => {
    try {
      const status = await apiRequest<LiveStatus>('/crypto-delta/live/status');
      setLive(status && status.limits && status.execution ? status : null);
      const list = await apiRequest<{ orders: LiveOrder[]; blocked_attempts?: Blocked[] }>('/crypto-delta/live/orders?limit=30');
      setOrders(Array.isArray(list?.orders) ? list.orders : []);
      setBlocked(Array.isArray(list?.blocked_attempts) ? list.blocked_attempts : []);
    } catch { /* live controls are optional: the paper test works without them */ }
  }, []);
  useEffect(() => { void load(); void loadLive(); }, [load, loadLive]);

  async function act<T>(fn: () => Promise<T>): Promise<T | undefined> {
    setBusy(true); setError('');
    try { return await fn(); } catch (e) { setError(messageOf(e)); return undefined; } finally { setBusy(false); }
  }
  const create = (event: FormEvent) => {
    event.preventDefault();
    void act(async () => {
      const made = await apiRequest<Forward>('/crypto-delta/forward-tests', { method: 'POST', body: JSON.stringify({ symbol, resolution: tf, rr, direction: Number(direction), contracts: Number(contracts) }) });
      setOpen(made); await load();
    });
  };
  const arm = () => void act(async () => { setLive(await apiRequest<LiveStatus>('/crypto-delta/live/arm', { method: 'POST', body: JSON.stringify({ acknowledged: true }) })); setConfirming(false); void refreshExecutionState(); });
  const killSwitch = () => void act(async () => { await apiRequest('/crypto-delta/live/disarm', { method: 'POST', body: '{}' }); await loadLive(); void refreshExecutionState(); });
  const setExecuteLive = (t: Forward, enabled: boolean) => {
    if (enabled && !window.confirm(`Let "${t.name}" place REAL Delta orders (1 contract, leveraged) when it signals?`)) return;
    void act(async () => { await apiRequest(`/crypto-delta/live/tests/${t.id}`, { method: 'PUT', body: JSON.stringify({ enabled }) }); await load(); await loadLive(); });
  };
  const show = (id: string) => void act(async () => setOpen(await apiRequest<Forward>(`/crypto-delta/forward-tests/${id}`)));
  const refresh = (id: string) => void act(async () => { setOpen(await apiRequest<Forward>(`/crypto-delta/forward-tests/${id}/refresh`, { method: 'POST', body: '{}' })); await load(); });
  const toggle = (t: Forward) => void act(async () => { await apiRequest(`/crypto-delta/forward-tests/${t.id}/${t.status === 'active' ? 'pause' : 'resume'}`, { method: 'POST', body: '{}' }); await load(); });
  const remove = (id: string) => {
    if (!window.confirm('Delete this paper test and its results?')) return;
    void act(async () => { await apiRequest<void>(`/crypto-delta/forward-tests/${id}`, { method: 'DELETE' }); if (open?.id === id) setOpen(null); await load(); });
  };

  return (
    <section className="crypto-forward" aria-label="Delta forward test">
      <h2>Forward test: 5 EMA alert candle</h2>
      <p className="crypto-sub">Hypothetical paper test on completed Delta candles, starting now. A paper test never mixes with real P&amp;L; whether it can send real orders is the Live switch and the state below. Fees are Delta&apos;s taker fee both ways plus slippage; GST and funding are not modelled. Decision support, not investment advice.</p>
      <form className="crypto-form" onSubmit={create}>
        <label>Contract<input value={symbol} readOnly aria-label="Contract" /></label>
        <label>Candle<select value={tf} onChange={(e) => setTf(e.target.value)}><option value="5m">5m</option><option value="15m">15m</option><option value="1h">1h</option></select></label>
        <label>Side<select value={direction} onChange={(e) => setDirection(e.target.value)}>{Object.entries(DIRECTIONS).map(([v, l]) => <option key={v} value={v}>{l}</option>)}</select></label>
        <label>Target (× stop distance)<input value={rr} onChange={(e) => setRr(e.target.value)} inputMode="decimal" /></label>
        <label>Contracts<input value={contracts} onChange={(e) => setContracts(e.target.value)} inputMode="numeric" /></label>
        <button className="primary-button" type="submit" disabled={busy}>{busy ? 'Working…' : 'Start paper test'}</button>
      </form>
      {error && <p role="alert" className="crypto-error">{error}</p>}
      {live && (
        <div className="crypto-live" aria-label="Live order controls">
          <h3>Live orders <ExecutionStateChip venue={live.execution} /></h3>
          <p className="crypto-sub" data-testid="account-mode"><strong>{accountLine(live.execution)}</strong>. Limits: {live.limits.max_contracts} contract, leverage {live.limits.leverage}, daily loss cap {live.limits.max_daily_loss_usd} USD, notional cap {live.limits.max_notional_usd} USD.</p>
          <p className="crypto-sub">{todayLine(live.execution)}</p>
          {live.execution.deployment_flag.enabled && live.credentials_present && !live.armed && (
            <p><button type="button" className="secondary-button" onClick={() => setConfirming(true)} disabled={busy}>Turn live orders on…</button></p>
          )}
          {live.armed && <button type="button" className="secondary-button" onClick={killSwitch} disabled={busy}>Kill switch: disarm and cancel unfilled orders</button>}
          {live.execution.effective.state !== 'OFF' && <p className="crypto-sub">Each forward test below has its own Live switch.</p>}
          <ArmConfirm open={confirming} venue="Delta Exchange India" strategies={live.limits.allowed_strategies} quantityCap={`${live.limits.max_contracts} contract per order (USD perpetuals)`} leverage={live.limits.leverage} tradingWindow={live.execution.window.text} busy={busy}
            onCancel={() => setConfirming(false)} onConfirm={arm} />
          <h4>Live order ledger (real orders only)</h4>
          {orders.length === 0 ? <p className="crypto-sub">No order has been sent to Delta.</p> : (
            <table className="crypto-table"><thead><tr><th>Time</th><th>Contract</th><th>Side</th><th>Lev</th><th>Status</th><th>Delta said</th></tr></thead>
              <tbody>{orders.map((o) => <tr key={o.id}><td>{when(o.created_at)}</td><td>{o.symbol}</td><td>{o.side}</td><td>{o.leverage}x</td><td>{o.status}</td><td>{o.block_reason ?? '—'}</td></tr>)}</tbody></table>
          )}
          <h4>Blocked attempts (never sent to Delta)</h4>
          {blocked.length === 0 ? <p className="crypto-sub">No attempt was blocked.</p> : (
            <table className="crypto-table"><thead><tr><th>Time</th><th>Contract</th><th>Side</th><th>Why it was blocked</th></tr></thead>
              <tbody>{blocked.map((o) => <tr key={o.id}><td>{when(o.created_at)}</td><td>{o.symbol}</td><td>{o.side}</td><td>{o.block_reason ?? '—'}</td></tr>)}</tbody></table>
          )}
        </div>
      )}
      {tests.length === 0 ? <p className="crypto-sub">No forward tests yet.</p> : (
        <table className="crypto-table">
          <thead><tr><th>Test</th><th>Mode</th><th>Status</th><th>Trades</th><th>Win rate</th><th>Net P&amp;L (USD)</th><th>Updated</th><th /></tr></thead>
          <tbody>{tests.map((t) => (
            <tr key={t.id}>
              <td><button type="button" className="link-button" onClick={() => show(t.id)}>{t.name}</button></td>
              <td data-testid="test-mode">{testModeLine(live?.execution, t.execute_live)}</td>
              <td>{t.status}{t.last_error ? ' · data issue' : ''}</td>
              <td>{t.summary?.closed_trades ?? '—'}</td>
              <td>{t.summary?.win_rate == null ? '—' : `${(t.summary.win_rate * 100).toFixed(1)}%`}</td>
              <td>{num(t.summary?.net_pnl)}</td>
              <td>{when(t.last_run_at)}</td>
              <td><button type="button" className="secondary-button" onClick={() => toggle(t)} disabled={busy}>{t.status === 'active' ? 'Pause' : 'Resume'}</button>{' '}
                {live && live.execution.effective.state !== 'OFF' && <><button type="button" className="secondary-button" onClick={() => setExecuteLive(t, !t.execute_live)} disabled={busy}>{t.execute_live ? 'Live: on' : 'Live: off'}</button>{' '}</>}
                <button type="button" className="secondary-button" onClick={() => remove(t.id)} disabled={busy}>Delete</button></td>
            </tr>))}
          </tbody>
        </table>
      )}
      {open && (
        <div className="crypto-forward-detail">
          <h3>{open.name} <button type="button" className="secondary-button" onClick={() => refresh(open.id)} disabled={busy}>Refresh now</button></h3>
          <p className="crypto-sub">Started {when(open.start_time)} IST · {open.contracts} contract(s) · target {open.params.rr}× · {DIRECTIONS[String(open.params.direction)]}</p>
          {open.last_error && <p role="alert" className="crypto-error">Latest data problem: {open.last_error}. Showing the last good result.</p>}
          {open.summary && <p>Closed {open.summary.closed_trades} · wins {open.summary.wins} · gross {num(open.summary.gross_pnl)} · fees {num(open.summary.fees)} · slippage {num(open.summary.slippage)} · net {num(open.summary.net_pnl)} · avg R {num(open.summary.avg_r)}</p>}
          {open.open_positions.length > 0 && (
            <p>Open (paper): {open.open_positions.map((p) => `${p.side} from ${num(p.entry_fill)}, stop ${num(p.stop_initial)}, target ${num(p.target)}, mark ${num(p.mark)}`).join('; ')}</p>
          )}
          {(open.trades ?? []).length === 0 ? <p className="crypto-sub">No closed trades yet. A trade needs an alert candle and then a break of its extreme.</p> : (
            <table className="crypto-table">
              <thead><tr><th>Entry (IST)</th><th>Side</th><th>Entry</th><th>Exit</th><th>Reason</th><th>Net (USD)</th><th>R</th></tr></thead>
              <tbody>{[...(open.trades ?? [])].reverse().map((t) => (
                <tr key={t.entry_time}><td>{when(t.entry_time)}</td><td>{t.side}</td><td>{num(t.entry_fill)}</td><td>{num(t.exit_fill)}</td><td>{t.exit_reason}</td><td>{num(t.net_pnl)}</td><td>{num(t.r_multiple)}</td></tr>))}
              </tbody>
            </table>
          )}
        </div>
      )}
      <R3PaperCard />
      <CryptoScanner />
    </section>
  );
}
