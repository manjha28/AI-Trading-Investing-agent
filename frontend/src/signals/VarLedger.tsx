import { useMemo, useState } from 'react';
import { Empty, Notice, Pill, Scroll } from './shared';
import { istTime, money, num, show, type Cfg } from './varFormat';

type Col = { key: string; label: string; get: (t: Cfg) => string | number; fmt?: (t: Cfg) => string };
const exitPrice = (t: Cfg): number => {
  const o = (t.charge_orders as Cfg[] | undefined)?.at(-1);
  return o && Number(o.units) ? Number(o.turnover) / Number(o.units) : NaN;
};
const COLS: Col[] = [
  { key: 'date', label: 'Date', get: (t) => String(t.date) },
  { key: 'instrument', label: 'Instrument', get: (t) => String(t.instrument) },
  { key: 'setup', label: 'Strategy', get: (t) => String(t.setup) },
  { key: 'direction', label: 'Dir', get: (t) => String(t.direction) },
  { key: 'entry_ts', label: 'Entry (IST)', get: (t) => String(t.entry_ts), fmt: (t) => istTime(t.entry_ts) },
  { key: 'exit_ts', label: 'Exit (IST)', get: (t) => String(t.exit_ts), fmt: (t) => istTime(t.exit_ts) },
  { key: 'entry', label: 'Entry px', get: (t) => Number(t.entry), fmt: (t) => show(t.entry) },
  { key: 'exit_px', label: 'Exit px', get: exitPrice, fmt: (t) => (Number.isFinite(exitPrice(t)) ? exitPrice(t).toFixed(2) : '—') },
  { key: 'qty', label: 'Qty (units)', get: (t) => Number(t.lots) * Number(t.lot_size), fmt: (t) => String(Number(t.lots) * Number(t.lot_size)) },
  { key: 'gross_pnl', label: 'Gross', get: (t) => Number(t.gross_pnl), fmt: (t) => money(t.gross_pnl) },
  { key: 'brokerage', label: 'Brokerage', get: (t) => Number(t.brokerage), fmt: (t) => money(t.brokerage) },
  { key: 'exchange_charge', label: 'Exchange', get: (t) => Number(t.exchange_charge), fmt: (t) => money(t.exchange_charge) },
  { key: 'ipft', label: 'IPFT', get: (t) => Number(t.ipft), fmt: (t) => money(t.ipft) },
  { key: 'sebi', label: 'SEBI', get: (t) => Number(t.sebi), fmt: (t) => money(t.sebi) },
  { key: 'stt', label: 'STT', get: (t) => Number(t.stt), fmt: (t) => money(t.stt) },
  { key: 'stamp_duty', label: 'Stamp', get: (t) => Number(t.stamp_duty), fmt: (t) => money(t.stamp_duty) },
  { key: 'gst', label: 'GST', get: (t) => Number(t.gst), fmt: (t) => money(t.gst) },
  { key: 'charges', label: 'Total charges', get: (t) => Number(t.charges), fmt: (t) => money(t.charges) },
  { key: 'net_pnl', label: 'Net', get: (t) => Number(t.net_pnl), fmt: (t) => money(t.net_pnl) },
  { key: 'r_multiple', label: 'R', get: (t) => Number(t.r_multiple), fmt: (t) => num(t.r_multiple) },
  { key: 'exit_reason', label: 'Exit reason', get: (t) => String(t.exit_reason) },
];

export function VarLedger({ trades, scheduleOk }: { trades: Cfg[]; scheduleOk: boolean }) {
  const [f, setF] = useState({ instrument: '', setup: '', direction: '', from: '', to: '', pl: '', regime: '' });
  const [sort, setSort] = useState<{ key: string; dir: 1 | -1 }>({ key: 'entry_ts', dir: 1 });
  const [open, setOpen] = useState<Cfg | null>(null);
  const uniq = (k: string) => [...new Set(trades.map((t) => String(t[k])))].sort();
  const rows = useMemo(() => {
    const col = COLS.find((c) => c.key === sort.key) ?? COLS[0];
    return trades.filter((t) => (!f.instrument || t.instrument === f.instrument) && (!f.setup || t.setup === f.setup) && (!f.direction || t.direction === f.direction)
      && (!f.regime || t.regime === f.regime) && (!f.from || String(t.date) >= f.from) && (!f.to || String(t.date) <= f.to)
      && (!f.pl || (f.pl === 'win' ? Number(t.net_pnl) > 0 : Number(t.net_pnl) <= 0)))
      .sort((a, b) => { const x = col.get(a), y = col.get(b); return (x < y ? -1 : x > y ? 1 : 0) * sort.dir; });
  }, [trades, f, sort]);
  if (trades.length === 0) return <Empty title="No trades">Every candidate was rejected or none formed. See the funnel.</Empty>;
  const set = (k: keyof typeof f) => (e: { target: { value: string } }) => setF({ ...f, [k]: e.target.value });
  return (
    <section className="sl-card" aria-label="Trade ledger">
      <div className="sl-card-head"><h2>Trade ledger <Pill tone="warn">HYPOTHETICAL</Pill></h2><span className="sl-muted">{rows.length} of {trades.length} trades</span></div>
      {!scheduleOk && <Notice tone="bad">Fee schedule unavailable: net figures in this ledger are not valid.</Notice>}
      <div className="sl-grid2">
        <label>Instrument<select value={f.instrument} onChange={set('instrument')}><option value="">All</option>{uniq('instrument').map((x) => <option key={x}>{x}</option>)}</select></label>
        <label>Setup<select value={f.setup} onChange={set('setup')}><option value="">All</option>{uniq('setup').map((x) => <option key={x}>{x}</option>)}</select></label>
        <label>Direction<select value={f.direction} onChange={set('direction')}><option value="">All</option><option>LONG</option><option>SHORT</option></select></label>
        <label>Net P/L<select value={f.pl} onChange={set('pl')}><option value="">All</option><option value="win">Winners</option><option value="loss">Losers / flat</option></select></label>
        <label>Regime<select value={f.regime} onChange={set('regime')}><option value="">All</option>{uniq('regime').map((x) => <option key={x}>{x}</option>)}</select></label>
        <label>From<input type="date" value={f.from} onChange={set('from')} /></label>
        <label>To<input type="date" value={f.to} onChange={set('to')} /></label>
      </div>
      <Scroll label="Trades"><table className="sl-table"><thead><tr>{COLS.map((c) => (
        <th key={c.key} aria-sort={sort.key === c.key ? (sort.dir === 1 ? 'ascending' : 'descending') : 'none'}>
          <button type="button" onClick={() => setSort({ key: c.key, dir: sort.key === c.key ? (sort.dir === 1 ? -1 : 1) : 1 })}>{c.label}{sort.key === c.key ? (sort.dir === 1 ? ' ▲' : ' ▼') : ''}</button></th>))}</tr></thead>
        <tbody>{rows.map((t, i) => (
          <tr key={i} tabIndex={0} onClick={() => setOpen(t)} onKeyDown={(e) => { if (e.key === 'Enter') setOpen(t); }} style={{ cursor: 'pointer' }}>
            {COLS.map((c) => <td key={c.key}>{c.fmt ? c.fmt(t) : String(c.get(t))}</td>)}</tr>))}</tbody></table></Scroll>
      {open && (
        <div role="dialog" aria-label="Trade diagnostics" className="sl-card">
          <div className="sl-card-head"><h3>{String(open.instrument)} · {String(open.setup)} · {String(open.direction)}</h3><button type="button" onClick={() => setOpen(null)}>Close</button></div>
          <p className="sl-muted">Reference: {show(open.reference)} · regime {show(open.regime)} · data level {show(open.data_level)} · delta quality {show(open.delta_quality)} · strategy v{show(open.strategy_version)} · variant {show(open.variant)} · exit model {show(open.exit_model)}</p>
          <h4>Conditions at signal time</h4>
          <ul>{((open.conditions ?? []) as [string, string, string][]).map((c, j) => <li key={j}>{c[1] === 'pass' ? '✓' : '✗'} <strong>{c[0]}</strong>: {c[2]}</li>)}</ul>
          <h4>Derivation (levels computed in Python)</h4>
          <Scroll label="Derivation"><table className="sl-table"><tbody>{Object.entries((open.derivation ?? {}) as Cfg).map(([k, v]) => <tr key={k}><th scope="row">{k}</th><td>{String(v)}</td></tr>)}</tbody></table></Scroll>
          <h4>Charges by order ({show(open.fee_schedule_version)})</h4>
          <Scroll label="Charges"><table className="sl-table"><thead><tr><th>Order</th><th>Side</th><th>Turnover</th><th>Brokerage</th><th>Exchange</th><th>IPFT</th><th>SEBI</th><th>STT</th><th>Stamp</th><th>GST</th><th>Total</th></tr></thead>
            <tbody>{((open.charge_orders ?? []) as Cfg[]).map((o) => <tr key={o.order_id}><td>{o.order_id}</td><td>{o.side}</td><td>{money(o.turnover)}</td>{['brokerage', 'exchange', 'ipft', 'sebi', 'stt', 'stamp', 'gst'].map((k) => <td key={k}>{money(o.rounded[k])}</td>)}<td>{money(o.total)}</td></tr>)}</tbody></table></Scroll>
          <p className="sl-muted">Per-order amounts are rounded individually for display; the trade total is rounded from the aggregated raw amounts (STT and stamp to the rupee). Unverified components: {((open.unverified_components ?? []) as string[]).join(', ') || 'none'}.</p>
        </div>
      )}
    </section>
  );
}
