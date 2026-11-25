import { useCallback, useEffect, useState } from 'react';
import { apiRequest } from '../api';
import { messageOf } from './cryptoApi';

type Order = { id: string; time: string; symbol: string; side: string; contracts: number; leverage: number; status: string; entry_price: string | null; stop_price: string | null; target_price: string | null;
  block_reason: string | null; outcome: string; outcome_label: string; exit_at: string | null; exit_price: string | null; realized_pnl: string | null; fees: string | null; funding: string | null; net_pnl: string | null };
type Position = { symbol: string; size: string; entry_price: string | null; mark_price: string | null; liquidation_price: string | null; unrealised_pnl: string | null; margin: string | null };
type OpenOrder = { id: number; symbol: string; side: string; size: string; type: string; limit_price: string | null; stop_price: string | null; stop_type: string | null };
type Report = { wallet?: { total: string; available: string; margin_in_use: string; unrealised_pnl: string | null }; positions?: Position[]; open_orders?: OpenOrder[];
  realized_pnl?: { today: string; all_time: string; all_time_note: string }; orders?: Order[]; synced_at: string | null; stale: boolean; error: string | null; disclaimer: string };

const REFRESH_MS = 5_000;
const usd = (v: string | null | undefined, digits = 4) => (v == null || Number.isNaN(Number(v)) ? '—' : `${Number(v) < 0 ? '-' : ''}$${Math.abs(Number(v)).toFixed(digits)}`);
const num = (v: string | null | undefined) => (v == null || Number.isNaN(Number(v)) ? '—' : Number(v).toLocaleString('en-US', { maximumFractionDigits: 2 }));
const when = (iso: string | null) => (iso ? iso.replace('T', ' ').slice(0, 19) + ' UTC' : '—');

/** Live Delta account as Delta reports it: wallet, positions, open orders, realised P&L and what became of every live order. Read only. */
export function CryptoLiveReport() {
  const [report, setReport] = useState<Report | null>(null);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(async () => {
    try { setReport(await apiRequest<Report>('/crypto-delta/live/report')); setError(null); } catch (e) { setError(messageOf(e)); }
  }, []);
  useEffect(() => {
    void load();
    const timer = window.setInterval(() => { if (document.visibilityState === 'visible') void load(); }, REFRESH_MS);
    return () => window.clearInterval(timer);
  }, [load]);
  const stale = error !== null || report?.stale === true;
  const w = report?.wallet;
  return (
    <section className="crypto-live-report" aria-label="Live execution report">
      <h3>Live execution report <span className="chip">Real money · read only</span></h3>
      {stale && <p role="alert" className="crypto-error">Stale: {error ?? report?.error ?? 'Delta could not be read'}. Showing the last copy from {when(report?.synced_at ?? null)}.</p>}
      {!report && !error && <p className="crypto-sub">Loading from Delta…</p>}
      {w && (
        <dl className="crypto-kpis" aria-label="Wallet">
          <div><dt>Total balance</dt><dd>{usd(w.total)}</dd></div>
          <div><dt>Available</dt><dd>{usd(w.available)}</dd></div>
          <div><dt>Margin in use</dt><dd>{usd(w.margin_in_use)}</dd></div>
          <div><dt>Unrealised P&amp;L</dt><dd>{usd(w.unrealised_pnl)}</dd></div>
          <div><dt>Realised today</dt><dd>{usd(report?.realized_pnl?.today)}</dd></div>
          <div><dt>Realised all-time</dt><dd>{usd(report?.realized_pnl?.all_time)}</dd></div>
        </dl>
      )}
      {report && <p className="crypto-sub">Last synced {when(report.synced_at)}; refreshes every few seconds while this page is open. {report.realized_pnl?.all_time_note}</p>}
      <h4>Open positions</h4>
      {report?.positions?.length ? (
        <table className="crypto-table"><thead><tr><th>Symbol</th><th>Size</th><th>Entry</th><th>Mark</th><th>Liquidation</th><th>Stop</th><th>Target</th><th>Unrealised P&amp;L</th></tr></thead>
          <tbody>{report.positions.map((p) => {
            const mine = (report.orders ?? []).find((o) => o.symbol === p.symbol && o.outcome === 'still_open');
            return <tr key={p.symbol}><td>{p.symbol}</td><td>{p.size}</td><td>{num(p.entry_price)}</td><td>{num(p.mark_price)}</td><td>{num(p.liquidation_price)}</td><td>{num(mine?.stop_price)}</td><td>{num(mine?.target_price)}</td><td>{usd(p.unrealised_pnl)}</td></tr>;
          })}</tbody></table>
      ) : <p className="crypto-sub">No open positions.</p>}
      <h4>Open orders</h4>
      {report?.open_orders?.length ? (
        <table className="crypto-table"><thead><tr><th>Order</th><th>Symbol</th><th>Side</th><th>Size</th><th>Type</th><th>Price</th></tr></thead>
          <tbody>{report.open_orders.map((o) => <tr key={o.id}><td>{o.id}</td><td>{o.symbol}</td><td>{o.side}</td><td>{o.size}</td><td>{o.stop_type ?? o.type}</td><td>{num(o.stop_price ?? o.limit_price)}</td></tr>)}</tbody></table>
      ) : <p className="crypto-sub">No open orders.</p>}
      <h4>Every live order and what became of it</h4>
      {report?.orders?.length ? (
        <table className="crypto-table"><thead><tr><th>Placed</th><th>Order</th><th>Entry</th><th>Stop / target</th><th>Outcome</th><th>Exit</th><th>Realised</th><th>Fees incl. GST</th><th>Funding</th><th>Net</th></tr></thead>
          <tbody>{report.orders.map((o) => (
            <tr key={o.id}><td>{when(o.time)}</td><td>{o.side} {o.contracts} {o.symbol} · {o.leverage}x</td><td>{num(o.entry_price)}</td><td>{num(o.stop_price)} / {num(o.target_price)}</td>
              <td>{o.status === 'rejected' || o.status === 'error' ? `Rejected: ${o.block_reason ?? '—'}` : o.outcome_label}</td><td>{o.exit_price ? `${num(o.exit_price)} · ${when(o.exit_at)}` : '—'}</td>
              <td>{usd(o.realized_pnl)}</td><td>{usd(o.fees)}</td><td>{usd(o.funding)}</td><td>{usd(o.net_pnl)}</td></tr>))}</tbody></table>
      ) : <p className="crypto-sub">No live orders yet.</p>}
      <p className="crypto-sub">Figures are Delta&apos;s own records. {report?.disclaimer ?? 'Decision support, not investment advice.'}</p>
    </section>
  );
}
