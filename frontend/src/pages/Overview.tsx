import { formatMoney, formatQuantity, type WorkspaceState } from '../components/useWorkspace';
import { PortfolioSummary } from '../components/PortfolioSummary';
import { TransactionEntry } from '../components/TransactionEntry';
import { ThesisEditor } from '../components/ThesisEditor';

type Props = Pick<
  WorkspaceState,
  | 'cancelEditingTransaction'
  | 'coverage'
  | 'addInstrumentToWatchlist'
  | 'csvContent'
  | 'csvPreview'
  | 'currency'
  | 'editingTransaction'
  | 'exportTransactionCsv'
  | 'importTransactionCsv'
  | 'importingCsv'
  | 'loadTransactionCsv'
  | 'loadingPortfolio'
  | 'marketDataStatus'
  | 'portfolioId'
  | 'previewTransactionCsv'
  | 'refreshPortfolioValuation'
  | 'refreshingValuation'
  | 'removeInstrumentFromWatchlist'
  | 'riskMetrics'
  | 'setActiveView'
  | 'setCoverageVersion'
  | 'setThesisSymbol'
  | 'setTransactionType'
  | 'submitTransaction'
  | 'summary'
  | 'thesisSymbol'
  | 'transactionType'
  | 'valuation'
  | 'valuationBySymbol'
  | 'watchlist'
>;

export function OverviewPage({ workspace }: { workspace: Props }) {
  const {
    coverage,
    currency,
    addInstrumentToWatchlist,
    loadingPortfolio,
    marketDataStatus,
    portfolioId,
    removeInstrumentFromWatchlist,
    setActiveView,
    setCoverageVersion,
    setThesisSymbol,
    summary,
    thesisSymbol,
    valuation,
    valuationBySymbol,
    watchlist,
  } = workspace;
  const instrumentIdForSymbol = (symbol: string) =>
    valuationBySymbol.get(symbol)?.instrument_id
    ?? coverage?.managed.find((entry) => entry.symbol === symbol)?.instrument_id
    ?? coverage?.unmanaged.find((entry) => entry.symbol === symbol)?.instrument_id
    ?? null;

  return (
    <div className="overview-page">
      <div className="overview-summary">
        <PortfolioSummary {...workspace} showRisk />
      </div>
      {!loadingPortfolio && summary && <>
        <div className="content-grid overview-workspace-grid">
          <section className="data-section">
            <div className="section-heading"><div><p className="eyebrow">CURRENT EXPOSURE</p><h2>Open positions</h2></div><span className="section-count">{summary.positions.length} records</span></div>
            {summary.positions.length === 0 ? <div className="table-empty">No open positions recorded.</div> : (
              <div className="table-scroll"><table><thead><tr><th>Instrument</th><th>Quantity</th><th>Held since</th><th>Average cost</th><th>Cost basis</th><th>Last price</th><th>Market value</th><th>Unrealized P&amp;L</th><th>Day change</th><th>Weight</th><th>Data quality</th><th>Thesis</th><th>Watchlist</th></tr></thead><tbody>{summary.positions.map((position) => {
                const priced = valuationBySymbol.get(position.symbol);
                const needsAttention = !priced || priced.freshness === 'stale' || priced.is_synthetic || priced.freshness === 'unavailable';
                const isWatchlisted = Boolean(priced?.instrument_id && watchlist.some((item) => item.instrument_id === priced.instrument_id));
                return <tr key={position.symbol}>
                  <td className="instrument-cell">{position.symbol}</td>
                  <td>{formatQuantity(position.quantity)}</td>
                  <td>{position.holding_period_start ? new Date(position.holding_period_start).toLocaleDateString() : '—'}</td>
                  <td>{formatMoney(position.average_cost, currency)}</td>
                  <td>{formatMoney(position.cost_basis, currency)}</td>
                  <td>{priced?.last_price ? formatMoney(priced.last_price, currency) : '—'}</td>
                  <td>{priced?.market_value ? formatMoney(priced.market_value, currency) : '—'}</td>
                  <td className={priced?.unrealized_pnl === null || priced?.unrealized_pnl === undefined ? '' : Number(priced.unrealized_pnl) >= 0 ? 'positive' : 'negative'}>{priced?.unrealized_pnl ? formatMoney(priced.unrealized_pnl, currency) : '—'}</td>
                  <td>{priced?.day_change ? formatMoney(priced.day_change, currency) : '—'}{priced?.day_change_percent ? ` (${Number(priced.day_change_percent).toFixed(2)}%)` : ''}</td>
                  <td>{priced?.weight_percent ? `${Number(priced.weight_percent).toFixed(2)}%` : '—'}</td>
                  <td>{needsAttention ? <span className={`freshness-badge ${priced?.is_synthetic ? 'freshness-synthetic' : `freshness-${priced?.freshness ?? 'unavailable'}`}`}>{priced?.is_synthetic ? 'synthetic' : priced?.freshness ?? 'unlinked'}</span> : <span className="freshness-badge freshness-fresh">live</span>}</td>
                  <td>{(() => {
                    const managed = coverage?.managed.find((entry) => entry.symbol === position.symbol);
                    return <button className="secondary-button thesis-button" type="button" onClick={() => setThesisSymbol(position.symbol)}>{coverage === null ? 'Thesis' : managed ? (managed.review_overdue ? 'Review due' : 'Edit thesis') : 'Unmanaged · add thesis'}</button>;
                  })()}</td>
                  <td>{priced?.instrument_id && <button className="text-button watchlist-row-action" type="button" onClick={() => void (isWatchlisted ? removeInstrumentFromWatchlist(priced.instrument_id!) : addInstrumentToWatchlist(priced.instrument_id!, position.symbol))}>{isWatchlisted ? 'Remove' : 'Add'}</button>}</td>
                </tr>;
              })}</tbody></table></div>
            )}
            {coverage && coverage.unmanaged.length > 0 && <section className="unmanaged-positions" aria-label="Unmanaged positions">
              <h3>Unmanaged positions</h3>
              {coverage.unmanaged.map((entry) => <article className="unmanaged-position" key={entry.symbol}>
                <div><strong>{entry.symbol}</strong><span>{entry.reason === 'no_instrument_linked' ? 'Link this holding to a market instrument before creating its thesis.' : entry.reason === 'thesis_not_active' ? `Most recent thesis is ${entry.thesis_status ?? 'not active'}; create or reactivate an active thesis.` : 'No active thesis exists for this holding.'}</span></div>
                <button className="secondary-button" type="button" onClick={() => setThesisSymbol(entry.symbol)}>{entry.instrument_id ? 'Open thesis editor' : 'Link instrument / thesis'}</button>
              </article>)}
            </section>}
            {thesisSymbol && portfolioId && <ThesisEditor portfolioId={portfolioId} currency={currency} symbol={thesisSymbol} instrumentId={instrumentIdForSymbol(thesisSymbol)} onClose={() => setThesisSymbol(null)} onChanged={() => setCoverageVersion((value) => value + 1)} />}
            <div className="valuation-footnote"><span className={`status-dot ${marketDataStatus}`} /> {valuation?.confidence === 'unreliable' ? `Valuation is unreliable: ${valuation.reasons.join(' ')}` : `Open cost basis ${formatMoney(summary.invested_cost, currency)} · market prices are not FX-converted.`}</div>
          </section>
          <TransactionEntry {...workspace} />
        </div>
        <section className="data-section watchlist-section">
          <div className="section-heading">
            <div><p className="eyebrow">MARKET MONITORING</p><h2>Watchlist</h2></div>
            <button className="text-button" type="button" onClick={() => setActiveView('market-data')}>Open markets</button>
          </div>
          {watchlist.length === 0 ? <div className="table-empty">No instruments on your watchlist. Add one from Markets to see its cached quote and data quality here.</div> : (
            <div className="table-scroll"><table><thead><tr><th>Instrument</th><th>Market</th><th>Last cached quote</th><th>Source</th><th>Data quality</th><th>Retrieved</th><th /></tr></thead><tbody>
              {watchlist.map((item) => <tr key={item.instrument_id}>
                <td className="instrument-cell">{item.symbol}<small className="trade-subtext">{item.display_name}</small></td>
                <td>{item.venue} · {item.asset_class}</td>
                <td>{item.quote ? formatMoney(item.quote.close, item.currency) : '—'}</td>
                <td>{item.quote?.source ?? 'No cached quote'}</td>
                <td>
                  {item.relink_required ? <span className="freshness-badge freshness-unavailable">relink required</span> : item.is_synthetic || item.quote?.is_synthetic ? <span className="freshness-badge freshness-synthetic">synthetic</span> : item.quote ? <span className={`freshness-badge freshness-${item.quote.freshness}`}>{item.quote.freshness}{item.quote.is_verified ? ' · verified' : ''}</span> : <span className="freshness-badge freshness-unavailable">unavailable</span>}
                </td>
                <td>{item.quote ? new Date(item.quote.retrieved_at).toLocaleString() : '—'}</td>
                <td><button className="text-button watchlist-row-action" type="button" onClick={() => void removeInstrumentFromWatchlist(item.instrument_id)}>Remove</button></td>
              </tr>)}
            </tbody></table></div>
          )}
          <div className="valuation-footnote">Cached quote data only; this overview does not request a fresh quote. Synthetic data is clearly labeled.</div>
        </section>
      </>}
    </div>
  );
}
