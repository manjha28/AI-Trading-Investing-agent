import { CircleDollarSign, ShieldCheck } from 'lucide-react';
import { formatMoney, formatQuantity, type WorkspaceState } from './useWorkspace';

type Props = Pick<
  WorkspaceState,
  | 'portfolioId'
  | 'valuation'
  | 'refreshingValuation'
  | 'refreshPortfolioValuation'
  | 'loadingPortfolio'
  | 'summary'
  | 'currency'
  | 'riskMetrics'
> & { showRisk: boolean };

export function PortfolioSummary({
  portfolioId,
  valuation,
  refreshingValuation,
  refreshPortfolioValuation,
  loadingPortfolio,
  summary,
  currency,
  riskMetrics,
  showRisk,
}: Props) {
  return (
    <>
      <div className="valuation-toolbar">
        <div className="data-note"><CircleDollarSign size={17} /><span>{valuation?.confidence === 'unreliable' ? valuation.reasons.join(' ') : 'Portfolio prices are shown with freshness and source provenance; synthetic data is never presented as live.'}</span></div>
        <button className="secondary-button" type="button" onClick={() => void refreshPortfolioValuation()} disabled={refreshingValuation || !portfolioId}>
          {refreshingValuation ? 'Refreshing…' : 'Refresh prices'}
        </button>
        <span className="last-updated">Updated {valuation?.last_updated ? new Date(valuation.last_updated).toLocaleString() : '—'}</span>
      </div>
      {loadingPortfolio || !summary ? <p className="loading-inline">Loading persisted portfolio data…</p> : (
        <>
          <section className="metrics-row portfolio-metrics" aria-label="Portfolio balances">
            <article className="metric"><span>Cash balance</span><strong>{formatMoney(summary.cash_balance, currency)}</strong><small>After recorded fees</small></article>
            <article className="metric"><span>Invested cost</span><strong>{formatMoney(summary.invested_cost, currency)}</strong><small>Open positions · ledger basis</small></article>
            <article className="metric"><span>Market value</span><strong>{valuation ? formatMoney(valuation.market_value, currency) : 'Unavailable'}</strong><small>{valuation?.confidence === 'unreliable' ? 'Partial or synthetic prices' : `${summary.positions.length} open positions`}</small></article>
            <article className="metric"><span>Total value</span><strong>{valuation ? formatMoney(valuation.total_value, currency) : 'Unavailable'}</strong><small>Cash + priced holdings · no FX</small></article>
            <article className="metric"><span>Unrealized P&amp;L</span><strong className={valuation && Number(valuation.unrealized_pnl) >= 0 ? 'positive' : 'negative'}>{valuation ? formatMoney(valuation.unrealized_pnl, currency) : 'Unavailable'}</strong><small>{valuation?.confidence === 'unreliable' ? 'Unreliable valuation' : 'Based on last prices'}</small></article>
            <article className="metric"><span>Day change</span><strong className={valuation && Number(valuation.day_change) >= 0 ? 'positive' : 'negative'}>{valuation ? formatMoney(valuation.day_change, currency) : 'Unavailable'}</strong><small>Quoted open-to-last move</small></article>
            <article className="metric"><span>Realized P&amp;L</span><strong className={Number(summary.realized_pnl) >= 0 ? 'positive' : 'negative'}>{formatMoney(summary.realized_pnl, currency)}</strong><small>Sales and recorded cash income</small></article>
          </section>
          {showRisk && <section className="data-section risk-metrics-card">
            <div className="section-heading"><div><p className="eyebrow">LEDGER-DERIVED RISK</p><h2>Portfolio risk</h2></div><ShieldCheck size={18} /></div>
            {!riskMetrics ? <p className="loading-inline">Loading risk metrics…</p> : <>
              <div className="risk-summary">
                <article><span>Top-three concentration</span><strong>{riskMetrics.top_three_concentration_pct === null ? 'Unavailable' : `${Number(riskMetrics.top_three_concentration_pct).toFixed(2)}%`}</strong></article>
                <article><span>Cash allocation</span><strong>{riskMetrics.cash_pct === null ? 'Unavailable' : `${Number(riskMetrics.cash_pct).toFixed(2)}%`}</strong></article>
                <article><span>HHI</span><strong>{riskMetrics.hhi === null ? 'Unavailable' : Number(riskMetrics.hhi).toFixed(4)}</strong></article>
              </div>
              {riskMetrics.concentration_unavailable_reason && <p className="valuation-footnote">{riskMetrics.concentration_unavailable_reason}</p>}
              <div className="risk-exposure">
                <h3>Currency exposure · no FX conversion</h3>
                {riskMetrics.currency_exposure.length === 0 ? <p>No open position exposure.</p> : riskMetrics.currency_exposure.map((item) => <p key={item.currency}>
                  <strong>{item.currency}</strong> · {item.position_count} position{item.position_count === 1 ? '' : 's'} · fresh, non-synthetic priced subtotal {item.market_value === null ? 'unavailable' : formatMoney(item.market_value, item.currency)} · {item.synthetic_positions} synthetic · {item.stale_positions} stale/unavailable
                </p>)}
              </div>
              <div className="risk-exposure">
                <h3>365-day holding-period boundary</h3>
                {riskMetrics.ltcg_boundary_positions.length === 0 ? <p>No open acquisition lots within 30 days of the 365-day boundary.</p> : riskMetrics.ltcg_boundary_positions.map((position) => <p key={position.symbol}>
                  <strong>{position.symbol}</strong> · {position.ltcg_boundary_lots.map((lot) => `${lot.days_held} days held (${formatQuantity(lot.quantity)} shares)`).join(', ')}
                </p>)}
                <small>{riskMetrics.tax_disclaimer}</small>
              </div>
              <p className="valuation-footnote">{riskMetrics.disclaimer}</p>
            </>}
          </section>}
        </>
      )}
    </>
  );
}
