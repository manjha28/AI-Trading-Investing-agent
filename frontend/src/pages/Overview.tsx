import { useEffect, useState } from 'react';
import { apiRequest } from '../api';
import { formatMoney, formatQuantity, type WorkspaceState } from '../components/useWorkspace';
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

type DhanPortfolio = {
  source: 'dhan';
  currency: 'INR';
  synced_at: string | null;
  holdings: {
    symbol: string;
    exchange: string;
    security_id: string | null;
    isin: string | null;
    quantity: string;
    average_price: string | null;
    synced_at: string;
  }[];
  positions: {
    symbol: string;
    exchange: string;
    product: string;
    security_id: string | null;
    quantity: string;
    average_price: string | null;
    synced_at: string;
  }[];
  funds: {
    available_balance: string;
    sod_limit: string | null;
    collateral_amount: string | null;
    receivable_amount: string | null;
    utilized_amount: string | null;
    blocked_payout_amount: string | null;
    withdrawable_balance: string | null;
    synced_at: string;
  } | null;
};

type BrokerPortfolioSummary = {
  currency: 'INR';
  source: string | null;
  sources: string[];
  synced_at: string | null;
  totals: {
    cash: string | null;
    invested_cost: string | null;
    market_value: string | null;
    total_value: string | null;
    unrealized_pnl: string | null;
    day_change: string | null;
    synced_at: string | null;
  };
  brokers: {
    source: string;
    currency: 'INR';
    synced_at: string | null;
    cash: string | null;
    invested_cost: string | null;
    market_value: string | null;
    total_value: string | null;
    unrealized_pnl: string | null;
    day_change: string | null;
    holdings_count: number;
    positions_count: number;
  }[];
};

export function OverviewPage({ workspace }: { workspace: Props }) {
  const [dhanPortfolio, setDhanPortfolio] = useState<DhanPortfolio | null>(null);
  const [brokerSummary, setBrokerSummary] = useState<BrokerPortfolioSummary | null>(null);
  const [dhanLoading, setDhanLoading] = useState(true);
  const [dhanSyncing, setDhanSyncing] = useState(false);
  const [dhanError, setDhanError] = useState<string | null>(null);
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
  useEffect(() => {
    let cancelled = false;
    void Promise.all([
      apiRequest<DhanPortfolio>('/brokers/dhan/portfolio'),
      apiRequest<BrokerPortfolioSummary>('/brokers/portfolio-summary'),
    ])
      .then(([dhan, summary]) => {
        if (!cancelled) {
          setDhanPortfolio(dhan);
          setBrokerSummary(summary);
        }
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setDhanError(error instanceof Error ? error.message : 'Broker account data is unavailable.');
        }
      })
      .finally(() => {
        if (!cancelled) setDhanLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);
  const syncDhanPortfolio = async () => {
    setDhanSyncing(true);
    setDhanError(null);
    try {
      await apiRequest('/brokers/dhan/sync', { method: 'POST' });
      const [dhan, summary] = await Promise.all([
        apiRequest<DhanPortfolio>('/brokers/dhan/portfolio'),
        apiRequest<BrokerPortfolioSummary>('/brokers/portfolio-summary'),
      ]);
      setDhanPortfolio(dhan);
      setBrokerSummary(summary);
    } catch (error) {
      setDhanError(error instanceof Error ? error.message : 'Dhan account sync failed.');
    } finally {
      setDhanSyncing(false);
    }
  };
  const instrumentIdForSymbol = (symbol: string) =>
    valuationBySymbol.get(symbol)?.instrument_id
    ?? coverage?.managed.find((entry) => entry.symbol === symbol)?.instrument_id
    ?? coverage?.unmanaged.find((entry) => entry.symbol === symbol)?.instrument_id
    ?? null;

  return (
    <div className="overview-page">
      <section className="data-section" aria-label="Broker-sourced portfolio totals">
        <div className="section-heading">
          <div><p className="eyebrow">BROKER MIRROR · INR · READ ONLY</p><h2>Portfolio totals</h2></div>
          <span className="broker-sync-time">
            {dhanLoading ? 'Loading broker data…' : brokerSummary?.sources.length
              ? `Sources: ${brokerSummary.sources.join(', ')}`
              : 'No synced broker totals'}
          </span>
        </div>
        {dhanError && <p className="broker-sync-time" role="alert">{dhanError}</p>}
        <section className="metrics-row portfolio-metrics" aria-label="Combined broker totals">
          {([
            ['Cash balance', 'cash'],
            ['Invested cost', 'invested_cost'],
            ['Market value', 'market_value'],
            ['Total value', 'total_value'],
            ['Unrealized P&L', 'unrealized_pnl'],
            ['Day change', 'day_change'],
          ] as const).map(([label, field]) => {
            const value = brokerSummary?.totals[field] ?? null;
            const figureSources = brokerSummary?.brokers
              .filter((broker) => broker[field] !== null)
              .map((broker) => (
                `${broker.source}${broker.synced_at ? ` · synced ${new Date(broker.synced_at).toLocaleString()}` : ''}`
              )) ?? [];
            return <article className="metric" key={field}>
              <span>{label}</span>
              <strong>{value === null ? '—' : formatMoney(value, 'INR')}</strong>
              <small>
                {figureSources.length
                  ? figureSources.join(' + ')
                  : 'Broker mirror value unavailable'}
              </small>
            </article>;
          })}
        </section>
        <p className="valuation-footnote">
          Values come only from broker mirrors; no manual-ledger amounts or FX conversion are included. Market value, total value, unrealized P&amp;L and day change remain blank when the broker mirror does not provide them.
        </p>
        {!!brokerSummary?.brokers.length && <div className="table-scroll">
          <table>
            <thead><tr><th>Broker</th><th>Cash (INR)</th><th>Invested cost (INR)</th><th>Market value (INR)</th><th>Total value (INR)</th><th>Unrealized P&amp;L (INR)</th><th>Day change (INR)</th><th>Synced</th></tr></thead>
            <tbody>
              {brokerSummary.brokers.map((broker) => <tr key={broker.source}>
                <td className="instrument-cell">{broker.source}</td>
                <td>{broker.cash === null ? '—' : formatMoney(broker.cash, broker.currency)}</td>
                <td>{broker.invested_cost === null ? '—' : formatMoney(broker.invested_cost, broker.currency)}</td>
                <td>{broker.market_value === null ? '—' : formatMoney(broker.market_value, broker.currency)}</td>
                <td>{broker.total_value === null ? '—' : formatMoney(broker.total_value, broker.currency)}</td>
                <td>{broker.unrealized_pnl === null ? '—' : formatMoney(broker.unrealized_pnl, broker.currency)}</td>
                <td>{broker.day_change === null ? '—' : formatMoney(broker.day_change, broker.currency)}</td>
                <td>{broker.synced_at ? new Date(broker.synced_at).toLocaleString() : '—'}</td>
              </tr>)}
              <tr>
                <th>Combined</th>
                {(['cash', 'invested_cost', 'market_value', 'total_value', 'unrealized_pnl', 'day_change'] as const).map((field) => {
                  const value = brokerSummary.totals[field];
                  return <td key={field}>{value === null ? '—' : formatMoney(value, 'INR')}</td>;
                })}
                <td>{brokerSummary.totals.synced_at ? new Date(brokerSummary.totals.synced_at).toLocaleString() : '—'}</td>
              </tr>
            </tbody>
          </table>
        </div>}
      </section>
      {!loadingPortfolio && summary && <>
        <section className="data-section" aria-label="Dhan read-only portfolio">
          <div className="section-heading">
            <div><p className="eyebrow">READ-ONLY BROKER DATA · INR</p><h2>Dhan account</h2></div>
            <button className="secondary-button" type="button" disabled={dhanSyncing} onClick={() => void syncDhanPortfolio()}>
              {dhanSyncing ? 'Syncing…' : 'Sync Dhan'}
            </button>
          </div>
          {dhanError && <p className="broker-sync-time" role="alert">{dhanError}</p>}
          {dhanLoading ? <div className="table-empty">Loading stored Dhan account data…</div> : <>
            {dhanPortfolio?.synced_at && <p className="broker-sync-time">Source: Dhan · Last synced {new Date(dhanPortfolio.synced_at).toLocaleString()}</p>}
            <div className="content-grid">
              <section className="data-section">
                <div className="section-heading"><div><h3>Holdings</h3></div><span className="section-count">{dhanPortfolio?.holdings.length ?? 0} records</span></div>
                {!dhanPortfolio?.holdings.length ? <div className="table-empty">No Dhan holdings have been synced.</div> : <div className="table-scroll"><table><thead><tr><th>Instrument</th><th>Exchange</th><th>Quantity</th><th>Average cost</th><th>ISIN</th></tr></thead><tbody>{dhanPortfolio.holdings.map((holding) => <tr key={`${holding.security_id}-${holding.exchange}`}><td className="instrument-cell">{holding.symbol}<small className="trade-subtext">{holding.security_id ?? ''}</small></td><td>{holding.exchange}</td><td>{formatQuantity(holding.quantity)}</td><td>{holding.average_price ? formatMoney(holding.average_price, 'INR') : '—'}</td><td>{holding.isin ?? '—'}</td></tr>)}</tbody></table></div>}
              </section>
              <section className="data-section">
                <div className="section-heading"><div><h3>Open positions</h3></div><span className="section-count">{dhanPortfolio?.positions.length ?? 0} records</span></div>
                {!dhanPortfolio?.positions.length ? <div className="table-empty">No Dhan open positions have been synced.</div> : <div className="table-scroll"><table><thead><tr><th>Instrument</th><th>Exchange</th><th>Product</th><th>Net quantity</th><th>Cost price</th></tr></thead><tbody>{dhanPortfolio.positions.map((position) => <tr key={`${position.security_id}-${position.exchange}-${position.product}`}><td className="instrument-cell">{position.symbol}<small className="trade-subtext">{position.security_id ?? ''}</small></td><td>{position.exchange}</td><td>{position.product || '—'}</td><td>{formatQuantity(position.quantity)}</td><td>{position.average_price ? formatMoney(position.average_price, 'INR') : '—'}</td></tr>)}</tbody></table></div>}
              </section>
            </div>
          </>}
        </section>
        <div className="content-grid overview-workspace-grid">
          <section className="data-section">
            <div className="section-heading"><div><p className="eyebrow">MANUAL LEDGER · SEPARATE FROM BROKER TOTALS</p><h2>Ledger positions</h2></div><span className="section-count">{summary.positions.length} records</span></div>
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
                  <td>{needsAttention ? <span className={`freshness-badge ${priced?.is_synthetic ? 'freshness-synthetic' : `freshness-${priced?.freshness ?? 'unavailable'}`}`}>{priced?.is_synthetic ? 'synthetic' : priced?.freshness ?? 'unlinked'}</span> : <span className="freshness-badge freshness-fresh">verified · recent</span>}</td>
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
                  {item.relink_required ? <span className="freshness-badge freshness-unavailable">relink required</span> : item.is_synthetic || item.quote?.is_synthetic ? <span className="freshness-badge freshness-synthetic">synthetic</span> : item.quote ? <span className={`freshness-badge freshness-${item.quote.freshness_status}`}>{item.quote.freshness_status.replaceAll('_', ' ')}</span> : <span className="freshness-badge freshness-unavailable">unavailable</span>}
                </td>
                <td>{item.quote ? `${new Date(item.quote.market_timestamp).toLocaleString()} · ${Math.floor(item.quote.market_age_seconds)}s old` : '—'}</td>
                <td><button className="text-button watchlist-row-action" type="button" onClick={() => void removeInstrumentFromWatchlist(item.instrument_id)}>Remove</button></td>
              </tr>)}
            </tbody></table></div>
          )}
          <div className="valuation-footnote">Cached quote data only; this overview does not request a fresh quote. Provider mappings, timestamps and observation age determine whether prices can be used. Synthetic data is clearly labeled.</div>
        </section>
      </>}
    </div>
  );
}
