import { useCallback, useEffect, useState } from 'react';
import { apiRequest } from '../api';
import { messageOf } from './cryptoApi';

type Top = { symbol: string; turnover_usd: string; spread_bps: string; funding_rate: string | null };
type Scanner = { id: string; name: string; strategy_key?: string; resolution: string; symbols: string[]; status: string; label: string; wallet_usd: string; total_net_pnl: string | null; per_symbol: Record<string, { trades: number; net_pnl: string }> | null;
  signals_seen: number | null; signals_taken: number | null; execute_live?: boolean; live_block?: string | null; last_run_at: string | null; last_error: string | null; signal_log?: { symbol: string; side: string; entry_time: string; taken: boolean; reason: string }[]; live_note?: string };
const STRATEGY_OPTIONS = [
  { key: 'five_ema_alert', label: '5 EMA alert candle (paper)', note: 'Short/long on the alert candle breaking its own extreme; any resolution.' },
  { key: 'funding_extreme', label: 'Funding extremes (Unproven, frozen parameters)', note: 'Fades extreme funding on 1-hour bars; parameters are frozen and the result is unproven.' },
] as const;
const RESOLUTION_OPTIONS = ['5m', '15m', '1h'] as const;
const strategyLabel = (key?: string) => STRATEGY_OPTIONS.find((o) => o.key === key)?.label ?? key ?? '—';
const usd = (v: string | null | undefined) => (v == null || Number.isNaN(Number(v)) ? '—' : `${Number(v) < 0 ? '-' : ''}$${Math.abs(Number(v)).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`);

/** Multi-perpetual scanner: paper results; allowlisted scanners have a per-scanner Live switch (off by default). Several contracts feed one signal queue, sized by 0.5% wallet risk per trade, 5x leverage cap and a 2% total open-risk cap. */
export function CryptoScanner() {
  const [items, setItems] = useState<Scanner[]>([]);
  const [open, setOpen] = useState<Scanner | null>(null);
  const [text, setText] = useState('');
  const [strategy, setStrategy] = useState<string>('five_ema_alert');
  const [resolution, setResolution] = useState<string>('5m');
  const [top, setTop] = useState<Top[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(async () => { try { const r = await apiRequest<{ items?: Scanner[] }>('/delta-scanner'); setItems(Array.isArray(r?.items) ? r.items : []); } catch (e) { setError(messageOf(e)); } }, []);
  useEffect(() => { void load(); }, [load]);
  const act = async (fn: () => Promise<unknown>) => { setBusy(true); setError(null); try { await fn(); await load(); } catch (e) { setError(messageOf(e)); } finally { setBusy(false); } };
  const loadTop = () => act(async () => { setTop((await apiRequest<{ symbols: Top[] }>('/delta-scanner/universe/top?n=10')).symbols); });
  const create = (topN: number) => act(async () => { await apiRequest('/delta-scanner', { method: 'POST', body: JSON.stringify({ symbols: text, top_liquid: topN, strategy_key: strategy, resolution: strategy === 'funding_extreme' ? '1h' : resolution }) }); setText(''); });
  const setLive = (s: Scanner, enabled: boolean) => {
    if (enabled && !window.confirm(`Turn ${s.name} LIVE? New signals become REAL orders on your Delta account: 1 contract each, leveraged, with a stop and a target attached. They only go out while Delta live is armed (Overview switch). Expect losses as well as wins.`)) return;
    void act(async () => { await apiRequest(`/delta-scanner/${s.id}/live`, { method: 'PUT', body: JSON.stringify({ enabled }) }); });
  };
  const kill = (s: Scanner) => act(async () => { await apiRequest(`/delta-scanner/${s.id}/live/kill`, { method: 'POST', body: '{}' }); });
  const closeAll = (s: Scanner) => { if (window.confirm(`Close every open position ${s.name} opened on Delta, at market (reduce-only)? Live is turned off first.`)) void act(async () => { await apiRequest(`/delta-scanner/${s.id}/live/close-all`, { method: 'POST', body: '{}' }); }); };
  const show = (id: string) => act(async () => { setOpen(await apiRequest<Scanner>(`/delta-scanner/${id}`)); });
  return (
    <section className="crypto-scanner" aria-label="Multi-perpetual scanner">
      <h3>Multi-perpetual scanner <span className="chip">{items.some((i) => i.execute_live) ? 'Live switch on' : 'Paper'}</span></h3>
      <p className="crypto-sub">Runs the strategy you pick below on many perpetuals at once; every signal goes into one queue and is sized by 0.5% wallet risk per trade, a 5x effective leverage cap and a 2% total open-risk cap. Results here are hypothetical and kept apart from real P&amp;L. A scanner running the 5 EMA strategy on 5m or 15m candles has its own Live switch (off by default, needs a 2FA session): with it on and Delta live armed, new signals go through the same live gate as the live test (1 contract, leverage up to 25x, stop before liquidation, real daily-loss cap, one position per coin, total open risk within the scanner&apos;s cap). Other strategies stay paper only.</p>
      {error && <p role="alert" className="crypto-error">{error}</p>}
      <div className="crypto-form">
        <label>Strategy <select value={strategy} onChange={(e) => setStrategy(e.target.value)} aria-label="Scanner strategy">
          {STRATEGY_OPTIONS.map((o) => <option key={o.key} value={o.key}>{o.label}</option>)}</select></label>
        <label>Resolution <select value={strategy === 'funding_extreme' ? '1h' : resolution} disabled={strategy === 'funding_extreme'} onChange={(e) => setResolution(e.target.value)} aria-label="Scanner resolution">
          {RESOLUTION_OPTIONS.map((r) => <option key={r} value={r}>{r}</option>)}</select></label>
        <small className="crypto-sub">{STRATEGY_OPTIONS.find((o) => o.key === strategy)?.note}</small>
        <label>Add symbols (comma separated) <input value={text} onChange={(e) => setText(e.target.value)} placeholder="BTCUSD, ETHUSD, SOLUSD" aria-label="Symbols" /></label>
        <button type="button" className="secondary-button" disabled={busy || !text.trim()} onClick={() => void create(0)}>Start scanner on these</button>
        <button type="button" className="secondary-button" disabled={busy} onClick={() => void loadTop()}>Show top 10 most liquid</button>
        <button type="button" className="primary-button" disabled={busy} onClick={() => void create(10)}>Start on top 10 most liquid</button>
      </div>
      {top.length > 0 && (
        <table className="crypto-table"><caption>Top 10 by 24 h turnover with a spread under 10 bps (refreshed each time you press the button)</caption>
          <thead><tr><th>Symbol</th><th>Turnover (USD)</th><th>Spread (bps)</th><th>Funding</th></tr></thead>
          <tbody>{top.map((t) => <tr key={t.symbol}><td>{t.symbol}</td><td>{Number(t.turnover_usd).toLocaleString('en-US')}</td><td>{t.spread_bps}</td><td>{t.funding_rate ?? '—'}</td></tr>)}</tbody></table>
      )}
      {items.length === 0 ? <p className="crypto-sub">No scanner yet.</p> : (
        <table className="crypto-table"><thead><tr><th>Scanner</th><th>Strategy</th><th>Symbols</th><th>Status</th><th>Taken / seen</th><th>Net P&amp;L (paper, USD)</th><th>Live</th><th /></tr></thead>
          <tbody>{items.map((s) => <tr key={s.id}><td><button type="button" className="link-button" onClick={() => void show(s.id)}>{s.name}</button> <small>{s.label === 'unproven' ? 'Unproven' : 'Paper'}</small></td><td>{strategyLabel(s.strategy_key)} · {s.resolution}</td><td>{s.symbols.length}</td><td>{s.status}{s.last_error ? ' · some data missing' : ''}</td>
            <td>{s.signals_taken ?? '—'} / {s.signals_seen ?? '—'}</td><td>{usd(s.total_net_pnl)}</td>
            <td>{s.live_block ? <small title={s.live_block}>Paper only: {s.live_block}</small> : <>
              <button type="button" className={`live-switch-btn ${s.execute_live ? 'is-on' : ''}`} aria-pressed={!!s.execute_live} aria-label={`${s.name} live switch`} disabled={busy} onClick={() => setLive(s, !s.execute_live)}>{s.execute_live ? 'LIVE on' : 'Live off'}</button>{' '}
              <button type="button" className="live-switch-unarm" disabled={busy} onClick={() => void kill(s)} aria-label={`Kill switch for ${s.name}`}>Kill</button>{' '}
              <button type="button" className="secondary-button" disabled={busy} onClick={() => closeAll(s)} aria-label={`Close all positions of ${s.name}`}>Close all</button></>}</td>
            <td><button type="button" className="secondary-button" disabled={busy} onClick={() => void act(async () => { await apiRequest(`/delta-scanner/${s.id}/refresh`, { method: 'POST', body: '{}' }); })}>Refresh</button>{' '}
              <button type="button" className="secondary-button" disabled={busy} onClick={() => void act(async () => { await apiRequest(`/delta-scanner/${s.id}/status?status=${s.status === 'active' ? 'paused' : 'active'}`, { method: 'PUT', body: '{}' }); })}>{s.status === 'active' ? 'Pause' : 'Resume'}</button>{' '}
              <button type="button" className="secondary-button" disabled={busy} onClick={() => { if (window.confirm('Delete this scanner and its results?')) void act(async () => { await apiRequest(`/delta-scanner/${s.id}`, { method: 'DELETE' }); setOpen(null); }); }}>Delete</button></td></tr>)}</tbody></table>
      )}
      {open && (
        <div className="crypto-forward-detail">
          <h4>{open.name}</h4>
          <table className="crypto-table"><thead><tr><th>Symbol</th><th>Trades</th><th>Net P&amp;L (USD)</th></tr></thead>
            <tbody>{Object.entries(open.per_symbol ?? {}).map(([sym, v]) => <tr key={sym}><td>{sym}</td><td>{v.trades}</td><td>{usd(v.net_pnl)}</td></tr>)}<tr><th>Total</th><th /><th>{usd(open.total_net_pnl)}</th></tr></tbody></table>
          <h4>Signal log</h4>
          <table className="crypto-table"><thead><tr><th>Entry time</th><th>Symbol</th><th>Side</th><th>Taken</th><th>Reason</th></tr></thead>
            <tbody>{(open.signal_log ?? []).slice(-30).reverse().map((l, i) => <tr key={i}><td>{l.entry_time.replace('T', ' ').slice(0, 16)}</td><td>{l.symbol}</td><td>{l.side}</td><td>{l.taken ? 'yes' : 'no'}</td><td>{l.reason}</td></tr>)}</tbody></table>
          <p className="crypto-sub">{open.live_note} Decision support, not investment advice.</p>
        </div>
      )}
    </section>
  );
}
