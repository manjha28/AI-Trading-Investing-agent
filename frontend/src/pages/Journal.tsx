import {
  ArrowUpRight, CircleDollarSign, Clock3, Pencil, Plus, Search, ShieldCheck, Trash2, Wallet,
} from 'lucide-react';
import { CandleChart } from '../components/CandleChart';
import { ThesisEditor } from '../components/ThesisEditor';
import {
  formatMoney, formatQuantity, localDateTime, marketRangeParams, indicatorLabels,
  type WorkspaceState,
} from '../components/useWorkspace';

export function JournalPage({ workspace }: { workspace: Pick<WorkspaceState, 'cancelEditingTrade' | 'currency' | 'editingTrade' | 'journalAnalytics' | 'journalAssetClass' | 'journalBreakdown' | 'journalCurrency' | 'journalHasExit' | 'journalTrades' | 'removeJournalTrade' | 'setJournalAssetClass' | 'setJournalCurrency' | 'setJournalHasExit' | 'startEditingTrade' | 'submitJournalTrade' | 'valuation'> }) {
  const {
  cancelEditingTrade,
  currency,
  editingTrade,
  journalAnalytics,
  journalAssetClass,
  journalBreakdown,
  journalCurrency,
  journalHasExit,
  journalTrades,
  removeJournalTrade,
  setJournalAssetClass,
  setJournalCurrency,
  setJournalHasExit,
  startEditingTrade,
  submitJournalTrade,
  valuation
} = workspace;
  return (
              <>
                <div className="data-note"><CircleDollarSign size={17} /><span>Journal analytics use only entered trades, fees, multipliers, and timestamps. No market prices are fetched.</span></div>
                {journalAnalytics && <section className="metrics-row journal-metrics" aria-label="Journal performance">
                  <article className="metric"><span>Closed trades</span><strong>{journalAnalytics.closed_trades}</strong><small>{journalAnalytics.open_trades} still open</small></article>
                  <article className="metric"><span>Win rate</span><strong>{journalAnalytics.win_rate === null ? '—' : `${journalAnalytics.win_rate}%`}</strong><small>{journalAnalytics.wins} wins · {journalAnalytics.losses} losses</small></article>
                  <article className="metric"><span>Net realized P&amp;L · {journalCurrency}</span><strong className={Number(journalAnalytics.net_pnl) >= 0 ? 'positive' : 'negative'}>{formatMoney(journalAnalytics.net_pnl, journalCurrency)}</strong><small>Closed trades in this currency</small></article>
                  <article className="metric"><span>Max drawdown · {journalCurrency}</span><strong>{formatMoney(journalAnalytics.max_drawdown, journalCurrency)}</strong><small>Closed-trade sequence</small></article>
                </section>}
                <section className="data-section breakdown-section">
                  <div className="section-heading"><div><p className="eyebrow">PERFORMANCE BY STRATEGY</p><h2>Strategy breakdown</h2></div></div>
                  {journalBreakdown.length === 0 ? <div className="table-empty">No strategy results for this currency.</div> : <div className="table-scroll"><table><thead><tr><th>Strategy</th><th>Closed</th><th>Wins / losses</th><th>Net P&amp;L</th><th>Expectancy</th><th>Profit factor</th></tr></thead><tbody>{journalBreakdown.map((row) => <tr key={row.key}><td className="instrument-cell">{row.key}</td><td>{row.closed_trades}</td><td>{row.wins} / {row.losses}</td><td className={Number(row.net_pnl) >= 0 ? 'positive' : 'negative'}>{formatMoney(row.net_pnl, journalCurrency)}</td><td>{row.expectancy === null ? '—' : formatMoney(row.expectancy, journalCurrency)}</td><td>{row.profit_factor ?? '—'}</td></tr>)}</tbody></table></div>}
                </section>
                <div className="content-grid journal-grid">
                  <section className="data-section">
                    <div className="section-heading"><div><p className="eyebrow">TRADE HISTORY</p><h2>Recorded trades</h2></div><label className="journal-currency">Analytics currency<select value={journalCurrency} onChange={(event) => setJournalCurrency(event.target.value)}><option value="USD">USD</option><option value="INR">INR</option></select></label></div>
                    {journalTrades.length === 0 ? <div className="table-empty">No journal trades recorded.</div> : <div className="table-scroll"><table><thead><tr><th>Instrument</th><th>Market</th><th>Side</th><th>Size</th><th>Entry</th><th>Exit</th><th>Net P&amp;L</th><th /></tr></thead><tbody>{journalTrades.map((trade) => <tr key={trade.id}><td className="instrument-cell">{trade.instrument}<small className="trade-subtext">{trade.strategy || 'No strategy'}</small></td><td>{trade.asset_class}</td><td>{trade.side}</td><td>{formatQuantity(trade.quantity)} × {formatQuantity(trade.multiplier)}</td><td>{formatMoney(trade.entry_price, trade.currency)}</td><td>{trade.exit_price === null ? 'Open' : formatMoney(trade.exit_price, trade.currency)}</td><td className={trade.net_pnl === null ? '' : Number(trade.net_pnl) >= 0 ? 'positive' : 'negative'}>{trade.net_pnl === null ? '—' : formatMoney(trade.net_pnl, trade.currency)}</td><td><button className="icon-button" type="button" onClick={() => startEditingTrade(trade)} aria-label={`Edit ${trade.instrument} trade`}><Pencil size={15} /></button><button className="icon-button" type="button" onClick={() => removeJournalTrade(trade.id)} aria-label={`Delete ${trade.instrument} trade`}><Trash2 size={15} /></button></td></tr>)}</tbody></table></div>}
                    {journalAnalytics && <div className="valuation-footnote">Expectancy {journalAnalytics.expectancy === null ? '—' : formatMoney(journalAnalytics.expectancy, journalCurrency)} · Profit factor {journalAnalytics.profit_factor ?? '—'}</div>}
                  </section>
                  <section className="entry-section">
                    <div className="section-heading"><div><p className="eyebrow">{editingTrade ? 'EDIT ENTRY' : 'MANUAL ENTRY'}</p><h2>{editingTrade ? `Edit ${editingTrade.instrument}` : 'Log a trade'}</h2></div>{editingTrade ? <button className="text-button" type="button" onClick={cancelEditingTrade}>Cancel</button> : <Plus size={18} />}</div>
                    <form key={editingTrade?.id ?? 'new-trade'} className="form-stack compact" onSubmit={submitJournalTrade}>
                      <label>Market<select name="asset_class" value={journalAssetClass} onChange={(event) => setJournalAssetClass(event.target.value)}><option value="equity">Equity</option><option value="future">Future</option><option value="option">Option</option><option value="crypto">Crypto</option></select></label>
                      <label>Quote currency<select name="currency" defaultValue={editingTrade?.currency ?? 'USD'}><option value="USD">USD</option><option value="INR">INR</option></select></label>
                      <label>Instrument<input name="instrument" placeholder="Symbol or contract" maxLength={64} defaultValue={editingTrade?.instrument ?? ''} required /></label>
                      <div className="form-pair"><label>Side<select name="side" defaultValue={editingTrade?.side ?? 'long'}><option value="long">Long</option><option value="short">Short</option></select></label><label>Quantity<input name="quantity" type="number" min="0.00000001" step="0.00000001" defaultValue={editingTrade?.quantity ?? ''} required /></label></div>
                      <div className="form-pair"><label>Multiplier<input name="multiplier" type="number" min="0.00000001" step="0.00000001" defaultValue={editingTrade?.multiplier ?? '1'} required /></label><label>Entry price<input name="entry_price" type="number" min="0.00000001" step="0.00000001" defaultValue={editingTrade?.entry_price ?? ''} required /></label></div>
                      <label>Entry time<input name="entry_time" type="datetime-local" defaultValue={localDateTime(editingTrade?.entry_time) || new Date(Date.now() - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 16)} required /></label>
                      {journalHasExit && <><div className="form-pair"><label>Exit price<input name="exit_price" type="number" min="0.00000001" step="0.00000001" defaultValue={editingTrade?.exit_price ?? ''} required /></label><label>Exit time<input name="exit_time" type="datetime-local" defaultValue={localDateTime(editingTrade?.exit_time)} required /></label></div></>}
                      {journalAssetClass === 'option' && <><div className="form-pair"><label>Option type<select name="option_type" defaultValue={editingTrade?.option_type ?? 'call'}><option value="call">Call</option><option value="put">Put</option></select></label><label>Strike<input name="strike" type="number" min="0.00000001" step="0.00000001" defaultValue={editingTrade?.strike ?? ''} required /></label></div><label>Expiry<input name="expiry" type="date" defaultValue={editingTrade?.expiry?.slice(0, 10) ?? ''} required /></label></>}
                      <label>Fees paid<input name="fees" type="number" min="0" step="0.01" defaultValue={editingTrade?.fees ?? '0.00'} required /></label>
                      <label>Planned risk<input name="planned_risk" type="number" min="0.01" step="0.01" defaultValue={editingTrade?.planned_risk ?? ''} /></label>
                      <label>Strategy<input name="strategy" maxLength={100} defaultValue={editingTrade?.strategy ?? ''} /></label>
                      <label>Setup<input name="setup" maxLength={100} defaultValue={editingTrade?.setup ?? ''} /></label>
                      <label>Tags<input name="tags" placeholder="patient, breakout" defaultValue={editingTrade?.tags.join(', ') ?? ''} /></label>
                      <label>Notes<textarea name="notes" maxLength={4000} defaultValue={editingTrade?.notes ?? ''} rows={3} /></label>
                      <label className="checkbox-line"><input type="checkbox" checked={journalHasExit} onChange={(event) => setJournalHasExit(event.target.checked)} /> Trade is closed</label>
                      <button className="primary-button" type="submit"><Plus size={16} /> {editingTrade ? 'Update trade' : 'Save trade'}</button>
                    </form>
                  </section>
                </div>
              </>
  );
}
