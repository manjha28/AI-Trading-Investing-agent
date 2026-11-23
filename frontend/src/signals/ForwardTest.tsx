import { useCallback, useEffect, useMemo, useState } from 'react';
import { apiRequest } from '../api';
import { fmtMoney, fmtPct, messageOf } from './lab';
import { Field, Notice, Pill, Scroll } from './shared';
import type { RegistryStrategy } from './StrategyRegistry';
import { formatIST } from '../lib/formatIST';
import { ExecutionStateChip } from '../components/ExecutionStateChip';
import { ArmConfirm } from '../components/ArmConfirm';
import { refreshExecutionState } from '../hooks/useExecutionState';
import { todayLine, type VenueState } from '../lib/executionState';

type Limits = { max_order_quantity: number; allowed_strategies: string[]; product: string; segment: string; entry_window_ist: string[]; max_daily_loss_inr: string; max_trades_per_day: number; max_open_positions: number; max_notional_inr: string };
type LiveStatus = {
  deployment_enabled: boolean; environment_block: string | null; armed: boolean; armed_at: string | null; can_arm: boolean; limits: Limits;
  today: { orders: number; placed: number; blocked: number; rejected: number; realized_pnl: string }; confirmation_phrase: string; warning: string; execution: VenueState;
};
type Summary = { closed_trades: number; wins: number; losses: number; win_rate: number | null; gross_pnl: string; charges: string; slippage: string; net_pnl: string; by_symbol: Record<string, { trades: number; wins: number; net_pnl: string }> };
type UniverseRow = { instrument_id: string; symbol: string; status: string; reason: string | null; last_bar_close?: string; age_minutes?: number; bars?: number };
type Snapshot = {
  as_of?: string; session_open?: boolean; waiting?: string; warning?: string; label?: string; quantity?: number; universe?: UniverseRow[]; summary?: Summary;
  trades?: { symbol: string; side: string; qty: string; entry_time: string; exit_time: string; entry_fill: string; exit_fill: string; exit_reason: string; net_pnl: string; charges: string }[];
  open_positions?: { symbol: string; side: string; qty: string; entry_time: string; entry_fill: string; stop_initial: string | null; target: string | null; mark: string | null }[];
  live_orders_this_pass?: { symbol: string; side: string; status: string; reason: string | null; order_id: string | null }[];
};
type FT = { whole_market_watch_list?: boolean; id: string; name: string; strategy_key: string; status: string; quantity: number; start_date: string; execute_live: boolean; instrument_count: number; last_run_at: string | null; last_error: string | null; summary: Summary | null; waiting?: string | null; label: string; snapshot?: Snapshot; timeframe?: string };
type Order = { id: string; created_at: string; symbol: string; side: string; quantity: number; strategy_key: string; status: string; block_reason: string | null; reference_price: string; stop_price: string | null; target_price: string | null; entry_price: string | null; exit_price: string | null; exit_reason: string | null; realized_pnl_estimate: string | null; dhan_order_id: string | null; broker_response: unknown };
type Preset = { key: string; label: string; available: number };

const tone = (v: unknown) => { const n = Number(v); return !Number.isFinite(n) || n === 0 ? '' : n > 0 ? 'sl-pos' : 'sl-neg'; };
const when = (iso: string | null | undefined) => formatIST(iso, '—');
const POLL_MS = 30_000;

function LivePanel({ status, reload }: { status: LiveStatus | null; reload: () => void }) {
  const [phrase, setPhrase] = useState('');
  const [loss, setLoss] = useState('500');
  const [trades, setTrades] = useState('10');
  const [open, setOpen] = useState('3');
  const [notional, setNotional] = useState('50000');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);
  if (!status) return <p className="sl-muted">Loading live-execution status…</p>;
  const act = async (path: string, body?: unknown) => {
    setBusy(true); setError(null); setNote(null);
    try {
      const r = await apiRequest<Record<string, unknown>>(`/dhan-live/${path}`, { method: 'POST', ...(body ? { body: JSON.stringify(body) } : {}) });
      if (path === 'disarm') setNote(`Disarmed. ${String(r.cancelled ?? 0)} unfilled order(s) cancelled.${Array.isArray(r.filled_positions_still_open) && r.filled_positions_still_open.length ? ` Still open at the broker: ${(r.filled_positions_still_open as string[]).join(', ')} — exit them in the Dhan app.` : ''}`);
      setPhrase(''); setConfirming(false); reload(); void refreshExecutionState();
    } catch (e) { setError(messageOf(e)); } finally { setBusy(false); }
  };
  return (
    <section className="sl-card" aria-label="Live execution">
      <div className="sl-card-head"><h3>Live order execution (Dhan)</h3><ExecutionStateChip venue={status.execution} /></div>
      <Notice tone="warn">{status.warning}</Notice>
      <ul className="ft-limits" aria-label="Hard limits">
        <li>Quantity per order: <strong>{status.limits.max_order_quantity}</strong> share</li>
        <li>{status.limits.segment} · {status.limits.product} only</li>
        <li>Strategies: <strong>{status.limits.allowed_strategies.join(', ')}</strong></li>
        <li>Entries {status.limits.entry_window_ist[0]}–{status.limits.entry_window_ist[1]} IST, Mon–Fri</li>
        <li>Daily loss cap ₹{status.limits.max_daily_loss_inr} · {status.limits.max_trades_per_day} orders/day · {status.limits.max_open_positions} open · ₹{status.limits.max_notional_inr} per order</li>
      </ul>
      <p className="sl-muted">{todayLine(status.execution)} · estimated realised {fmtMoney(status.today.realized_pnl)} (the Dhan contract note is the authority).</p>
      {status.execution.effective.state !== "LIVE" && <Notice tone="info">{status.execution.label}: {status.execution.effective.state.replace(/_/g, " ")}. {status.execution.effective.reasons.join("; ")}.</Notice>}
      {!status.deployment_enabled && <Notice tone="info">Live execution is switched <strong>off on this deployment</strong>. To allow it, set <code>DHAN_LIVE_EXECUTION_ENABLED=true</code> in the backend environment and restart. Dhan also requires a static IP for order APIs, which a Codespace does not have.</Notice>}
      {status.environment_block && <Notice tone="info">Live orders are blocked in this environment ({status.environment_block}).</Notice>}
      {error && <Notice tone="bad">{error}</Notice>}
      {note && <Notice tone="ok">{note}</Notice>}
      {status.armed ? (
        <div className="sl-toolbar">
          <button type="button" className="ft-kill" disabled={busy} onClick={() => void act('disarm')}>KILL SWITCH — disarm and cancel unfilled orders</button>
          <button type="button" disabled={busy} onClick={() => void act('reconcile')}>Reconcile with Dhan</button>
        </div>
      ) : (
        <fieldset className="sl-card" disabled={busy || !status.can_arm}>
          <legend>Arm live execution (needs a two-factor-verified session)</legend>
          <div className="sl-row">
            <Field label="Daily loss cap (₹)"><input aria-label="Daily loss cap" value={loss} onChange={(e) => setLoss(e.target.value)} inputMode="decimal" /></Field>
            <Field label="Orders per day"><input aria-label="Orders per day" value={trades} onChange={(e) => setTrades(e.target.value)} inputMode="numeric" /></Field>
            <Field label="Open positions"><input aria-label="Open positions" value={open} onChange={(e) => setOpen(e.target.value)} inputMode="numeric" /></Field>
            <Field label="Max order value (₹)"><input aria-label="Max order value" value={notional} onChange={(e) => setNotional(e.target.value)} inputMode="decimal" /></Field>
          </div>
          <Field label={`Type “${status.confirmation_phrase}” to confirm`}><input aria-label="Confirmation" value={phrase} onChange={(e) => setPhrase(e.target.value)} autoComplete="off" /></Field>
          <button type="button" className="sl-primary" disabled={phrase !== status.confirmation_phrase} onClick={() => setConfirming(true)}>Arm live execution…</button>
        </fieldset>
      )}
      <ArmConfirm open={confirming} venue="Dhan" strategies={status.limits.allowed_strategies} quantityCap={`${status.limits.max_order_quantity} share per order (${status.limits.product}, ${status.limits.segment})`} tradingWindow={`Mon-Fri ${status.limits.entry_window_ist[0]}-${status.limits.entry_window_ist[1]} IST`} busy={busy} onCancel={() => setConfirming(false)}
        onConfirm={() => void act('arm', { confirmation: phrase, max_daily_loss_inr: loss, max_trades_per_day: Number(trades), max_open_positions: Number(open), max_notional_inr: notional })} />
    </section>
  );
}

export function ForwardTest() {
  const [status, setStatus] = useState<LiveStatus | null>(null);
  const [tests, setTests] = useState<FT[]>([]);
  const [defaultStart, setDefaultStart] = useState('');
  const [strategies, setStrategies] = useState<RegistryStrategy[]>([]);
  const [presets, setPresets] = useState<Preset[]>([]);
  const [orders, setOrders] = useState<Order[]>([]);
  const [blockedAttempts, setBlockedAttempts] = useState<{ id: string; created_at: string; symbol: string; side: string; quantity: number; strategy_key: string; block_reason: string | null }[]>([]);
  const [selected, setSelected] = useState<FT | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [form, setForm] = useState({ strategy_key: 'five_ema_alert', universe: 'master', quantity: '1', start_date: '', live: false, candle: '15', vwap: true });

  const loadStatus = useCallback(() => apiRequest<LiveStatus>('/dhan-live/status').then(setStatus).catch((e) => setError(messageOf(e))), []);
  const loadOrders = useCallback(() => apiRequest<{ orders: Order[]; blocked_attempts?: { id: string; created_at: string; symbol: string; side: string; quantity: number; strategy_key: string; block_reason: string | null }[] }>('/dhan-live/orders?limit=50').then((r) => { setOrders(r.orders); setBlockedAttempts(r.blocked_attempts ?? []); }).catch(() => undefined), []);
  const loadTests = useCallback(() => apiRequest<{ forward_tests: FT[]; default_start_date: string }>('/forward-tests').then((r) => { setTests(r.forward_tests); setDefaultStart(r.default_start_date); setForm((f) => (f.start_date ? f : { ...f, start_date: r.default_start_date })); }).catch((e) => setError(messageOf(e))), []);
  const open = useCallback((id: string) => apiRequest<FT>(`/forward-tests/${id}`).then(setSelected).catch((e) => setError(messageOf(e))), []);

  useEffect(() => {
    void loadStatus(); void loadTests(); void loadOrders();
    apiRequest<{ strategies: RegistryStrategy[] }>('/signals-lab/registry').then((r) => setStrategies(r.strategies)).catch(() => undefined);
    apiRequest<{ presets: Preset[] }>('/signals-lab/lab/universes').then((r) => setPresets(r.presets)).catch(() => undefined);
  }, [loadStatus, loadTests, loadOrders]);
  // keep the open test and ledger current while the page is visible
  useEffect(() => {
    const id = selected?.id;
    const timer = window.setInterval(() => { void loadStatus(); void loadOrders(); void loadTests(); if (id) void open(id); }, POLL_MS);
    return () => window.clearInterval(timer);
  }, [selected?.id, loadStatus, loadOrders, loadTests, open]);

  const liveCapable = status?.limits.allowed_strategies ?? [];
  const liveControls = !!status && status.execution.effective.state !== 'OFF';          // live-order controls exist only when the effective state is not OFF
  const canLive = liveControls && liveCapable.includes(form.strategy_key) && Number(form.quantity) === (status?.limits.max_order_quantity ?? 1);

  async function create() {
    setBusy(true); setError(null); setNotice(null);
    try {
      const body = { strategy_key: form.strategy_key, universe: form.universe, quantity: Number(form.quantity), start_date: form.start_date || undefined, execute_live: form.live && canLive,
        ...(form.strategy_key === 'five_ema_alert' ? { params: { timeframe_min: Number(form.candle), vwap_filter: form.vwap ? 1 : 0, ...(form.vwap ? { rr: 2 } : {}) } } : {}) };
      const made = await apiRequest<FT & { excluded?: { symbol: string }[]; instrument_total?: number }>('/forward-tests', { method: 'POST', body: JSON.stringify(body) });
      setNotice(`Created “${made.name}” from ${made.start_date}${made.whole_market_watch_list ? ` on the entire Market watch list (${made.instrument_total ?? '?'} symbols; their 1-minute data is refreshed in rotation, about every 7 minutes)` : ''}.${made.excluded?.length ? ` ${made.excluded.length} symbol(s) left out for lack of stored 1-minute data.` : ''} ${made.execute_live ? 'It is flagged for live orders but places none until you arm live execution.' : 'Paper only: no order is ever placed.'}`);
      await loadTests(); await open(made.id);
    } catch (e) { setError(messageOf(e)); } finally { setBusy(false); }
  }
  async function act(path: string, method = 'POST', body?: unknown) {
    if (!selected) return;
    setError(null);
    try {
      await apiRequest(`/forward-tests/${selected.id}${path}`, { method, ...(body ? { body: JSON.stringify(body) } : {}) });
      if (method === 'DELETE') { setSelected(null); setConfirmDelete(false); } else await open(selected.id);
      await loadTests();
    } catch (e) { setError(messageOf(e)); }
  }

  const snap = selected?.snapshot;
  const s = snap?.summary;
  const registryName = useMemo(() => Object.fromEntries(strategies.map((x) => [x.key, x.name])), [strategies]);

  return (
    <div className="sl-stack">
      <Notice tone="info">Forward tests run a strategy on <strong>live data</strong> from the start date, counting only completed candles. By default they are <strong>paper</strong>: hypothetical trades at a fixed quantity after Indian charges and slippage, never real P&amp;L. Live orders exist only for the two allowlisted strategies at quantity 1, and only when you arm them below.</Notice>
      {error && <Notice tone="bad">{error}</Notice>}
      {notice && <Notice tone="ok">{notice}</Notice>}
      <LivePanel status={status} reload={() => { void loadStatus(); void loadOrders(); }} />

      <section className="sl-card" aria-label="New forward test">
        <div className="sl-card-head"><h3>New forward test</h3></div>
        <div className="sl-row">
          <Field label="Strategy">
            <select aria-label="Strategy" value={form.strategy_key} onChange={(e) => setForm({ ...form, strategy_key: e.target.value })}>
              {strategies.map((x) => <option key={x.key} value={x.key}>{x.name}{liveCapable.includes(x.key) ? ' · live-capable' : ''}</option>)}
            </select>
          </Field>
          <Field label="Symbols">
            <select aria-label="Symbols" value={form.universe} onChange={(e) => setForm({ ...form, universe: e.target.value })}>
              {presets.map((p) => <option key={p.key} value={p.key}>{p.label} — {p.available} usable</option>)}
            </select>
          </Field>
          {form.strategy_key === 'five_ema_alert' && (
            <Field label="5-EMA candle"><select aria-label="5-EMA candle" value={form.candle} onChange={(e) => setForm({ ...form, candle: e.target.value })}><option value="5">5-minute</option><option value="15">15-minute</option></select></Field>
          )}
          <Field label="Quantity per trade"><input aria-label="Quantity" value={form.quantity} onChange={(e) => setForm({ ...form, quantity: e.target.value })} inputMode="numeric" /></Field>
          <Field label="Start date"><input aria-label="Start date" type="date" value={form.start_date || defaultStart} onChange={(e) => setForm({ ...form, start_date: e.target.value })} /></Field>
        </div>
        {form.strategy_key === 'five_ema_alert' && <label className="ft-live-flag"><input type="checkbox" aria-label="VWAP filter" checked={form.vwap} onChange={(e) => setForm({ ...form, vwap: e.target.checked })} /> Only trade with VWAP (sell below, buy above) at 1:2 — the closest-to-break-even 5-EMA variant in the study (out-of-sample profit factor 0.98 on a thin sample; still not net-positive)</label>}
        {liveControls && <label className="ft-live-flag"><input type="checkbox" checked={form.live && canLive} disabled={!canLive} onChange={(e) => setForm({ ...form, live: e.target.checked })} /> Also place live orders for this test (allowlisted strategies, quantity {status?.limits.max_order_quantity ?? 1}; still needs arming)</label>}
        <div className="sl-toolbar"><button type="button" className="sl-primary" disabled={busy || !form.strategy_key} onClick={() => void create()}>{busy ? 'Creating…' : 'Create forward test'}</button></div>
      </section>

      <section className="sl-card" aria-label="Forward tests">
        <div className="sl-card-head"><h3>Your forward tests</h3></div>
        {tests.length === 0 ? <p className="sl-muted">None yet.</p> : (
          <ul className="lab-history">
            {tests.map((t) => (
              <li key={t.id}><button type="button" className={selected?.id === t.id ? 'active' : ''} onClick={() => { setConfirmDelete(false); void open(t.id); }}>
                <strong>{t.name}</strong>
                <small>{t.status} · {t.whole_market_watch_list ? 'entire Market watch list' : `${t.instrument_count} symbols`} · from {t.start_date} · qty {t.quantity}{t.execute_live ? ' · LIVE-FLAGGED' : ' · paper'}{t.summary ? ` · ${t.summary.closed_trades} trades · net ${fmtMoney(t.summary.net_pnl)}` : ''}</small></button></li>
            ))}
          </ul>
        )}
      </section>

      {selected && (
        <section className="sl-card" aria-label="Forward test detail">
          <div className="sl-card-head">
            <h3>{selected.name} <Pill tone={selected.status === 'active' ? 'ok' : 'warn'}>{selected.status}</Pill> {selected.execute_live ? <Pill tone="bad">live-flagged</Pill> : <Pill tone="info">paper</Pill>}</h3>
            <div className="sl-row">
              <button type="button" onClick={() => void act('/refresh')}>Refresh now</button>
              {selected.status === 'active' ? <button type="button" onClick={() => void act('/pause')}>Pause</button> : <button type="button" onClick={() => void act('/resume')}>Resume</button>}
              {selected.execute_live
                ? <button type="button" onClick={() => void act('/execute-live', 'PUT', { enabled: false })}>Make paper-only</button>
                : liveControls && <button type="button" disabled={!liveCapable.includes(selected.strategy_key) || selected.quantity !== (status?.limits.max_order_quantity ?? 1)} onClick={() => void act('/execute-live', 'PUT', { enabled: true })}>Flag for live orders</button>}
              {confirmDelete
                ? <span role="group" aria-label="Confirm delete"><button type="button" className="ft-kill" onClick={() => void act('', 'DELETE')}>Delete for good</button> <button type="button" onClick={() => setConfirmDelete(false)}>Keep</button></span>
                : <button type="button" onClick={() => setConfirmDelete(true)}>Delete</button>}
            </div>
          </div>
          <p className="sl-muted">{registryName[selected.strategy_key] ?? selected.strategy_key} · {selected.timeframe ?? '—'} candles · quantity {selected.quantity} · from {selected.start_date} · last run {when(selected.last_run_at)}{snap?.session_open ? ' · market open' : ' · market closed'}</p>
          {selected.last_error && <Notice tone="bad">Last run failed: {selected.last_error}</Notice>}
          {snap?.waiting && <Notice tone="info">{snap.waiting}</Notice>}
          {snap?.warning && <Notice tone="warn">{snap.warning}</Notice>}
          {snap?.live_orders_this_pass && snap.live_orders_this_pass.length > 0 && <Notice tone="warn">This pass: {snap.live_orders_this_pass.map((o) => `${o.side.toUpperCase()} ${o.symbol} → ${o.status}${o.reason ? ` (${o.reason})` : ''}`).join('; ')}</Notice>}

          <h4>Data</h4>
          <Scroll label="Data freshness">
            <table className="sl-table" aria-label="Data freshness"><thead><tr><th>Symbol</th><th>Status</th><th>Newest bar closed</th><th className="r">Age (min)</th><th>Note</th></tr></thead>
              <tbody>{(snap?.universe ?? []).map((u) => (
                <tr key={u.instrument_id}><td>{u.symbol}</td><td><Pill tone={u.status === 'ok' ? 'ok' : 'warn'}>{u.status}</Pill></td><td>{when(u.last_bar_close)}</td><td className="r">{u.age_minutes ?? '—'}</td><td>{u.reason ?? ''}</td></tr>))}</tbody>
            </table>
          </Scroll>

          <h4>Paper results (hypothetical, after charges and slippage)</h4>
          {s ? (
            <dl className="reg-metrics" aria-label="Paper summary">
              <div><dt>Closed trades</dt><dd>{s.closed_trades}</dd></div>
              <div><dt>Win rate</dt><dd>{s.win_rate != null ? fmtPct(s.win_rate * 100, 1) : '—'}</dd></div>
              <div><dt>Gross</dt><dd>{fmtMoney(s.gross_pnl)}</dd></div>
              <div><dt>Charges</dt><dd>{fmtMoney(s.charges)}</dd></div>
              <div><dt>Slippage</dt><dd>{fmtMoney(s.slippage)}</dd></div>
              <div><dt>Net</dt><dd className={tone(s.net_pnl)}>{fmtMoney(s.net_pnl)}</dd></div>
            </dl>
          ) : <p className="sl-muted">No results yet.</p>}
          {snap?.open_positions && snap.open_positions.length > 0 && (
            <><h4>Open (hypothetical) positions</h4>
              <table className="sl-table" aria-label="Open positions"><thead><tr><th>Symbol</th><th>Side</th><th>Entry</th><th className="r">Entry price</th><th className="r">Stop</th><th className="r">Target</th><th className="r">Mark</th></tr></thead>
                <tbody>{snap.open_positions.map((p, i) => <tr key={i}><td>{p.symbol}</td><td>{p.side}</td><td>{when(p.entry_time)}</td><td className="r">{p.entry_fill}</td><td className="r">{p.stop_initial ?? '—'}</td><td className="r">{p.target ?? '—'}</td><td className="r">{p.mark ?? '—'}</td></tr>)}</tbody></table></>
          )}
          {snap?.trades && snap.trades.length > 0 && (
            <><h4>Closed trades</h4>
              <Scroll label="Closed trades"><table className="sl-table" aria-label="Closed trades"><thead><tr><th>Symbol</th><th>Side</th><th>Entry</th><th className="r">In</th><th className="r">Out</th><th>Exit</th><th className="r">Charges</th><th className="r">Net</th></tr></thead>
                <tbody>{[...snap.trades].reverse().map((t, i) => <tr key={i}><td>{t.symbol}</td><td>{t.side}</td><td>{when(t.entry_time)}</td><td className="r">{t.entry_fill}</td><td className="r">{t.exit_fill}</td><td>{t.exit_reason}</td><td className="r">{fmtMoney(t.charges)}</td><td className={`r ${tone(t.net_pnl)}`}>{fmtMoney(t.net_pnl)}</td></tr>)}</tbody></table></Scroll></>
          )}
        </section>
      )}

      <section className="sl-card" aria-label="Live order ledger">
        <div className="sl-card-head"><h3>Live order ledger (real orders only)</h3></div>
        {orders.length === 0 ? <p className="sl-muted">No order has been sent to Dhan. Attempts the guard-rails stopped are listed separately below.</p> : (
          <Scroll label="Live orders">
            <table className="sl-table"><thead><tr><th>Time</th><th>Symbol</th><th>Side</th><th className="r">Qty</th><th className="r">Ref</th><th className="r">Stop</th><th className="r">Target</th><th>Status</th><th className="r">Est. P&amp;L</th></tr></thead>
              <tbody>{orders.map((o) => (
                <tr key={o.id}><td>{when(o.created_at)}</td><td>{o.symbol}</td><td>{o.side}</td><td className="r">{o.quantity}</td><td className="r">{o.reference_price}</td><td className="r">{o.stop_price ?? '—'}</td><td className="r">{o.target_price ?? '—'}</td>
                  <td><Pill tone={o.status === 'placed' || o.status === 'filled' || o.status === 'closed' ? 'ok' : o.status === 'blocked' ? 'warn' : 'bad'} title={o.block_reason ?? undefined}>{o.status}</Pill>{o.block_reason ? <small> {o.block_reason}</small> : null}</td>
                  <td className={`r ${tone(o.realized_pnl_estimate)}`}>{o.realized_pnl_estimate != null ? fmtMoney(o.realized_pnl_estimate) : '—'}</td></tr>))}</tbody>
            </table>
          </Scroll>
        )}
      </section>

      <section className="sl-card" aria-label="Blocked attempts">
        <div className="sl-card-head"><h3>Blocked attempts (never sent to Dhan)</h3></div>
        {blockedAttempts.length === 0 ? <p className="sl-muted">No attempt was blocked.</p> : (
          <Scroll label="Blocked attempts table"><table className="sl-table"><thead><tr><th>Time</th><th>Symbol</th><th>Side</th><th className="r">Qty</th><th>Strategy</th><th>Why it was blocked</th></tr></thead>
            <tbody>{blockedAttempts.map((b) => <tr key={b.id}><td>{when(b.created_at)}</td><td>{b.symbol}</td><td>{b.side}</td><td className="r">{b.quantity}</td><td>{b.strategy_key}</td><td>{b.block_reason ?? '—'}</td></tr>)}</tbody></table></Scroll>
        )}
      </section>
    </div>
  );
}
