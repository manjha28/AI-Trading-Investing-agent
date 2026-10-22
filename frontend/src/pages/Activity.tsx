import { Pencil, Trash2 } from 'lucide-react';
import { formatMoney, formatQuantity, type WorkspaceState } from '../components/useWorkspace';
import { PortfolioSummary } from '../components/PortfolioSummary';
import { TransactionEntry } from '../components/TransactionEntry';

type Props = Pick<
  WorkspaceState,
  | 'cancelEditingTransaction'
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
  | 'riskMetrics'
  | 'removeTransaction'
  | 'setTransactionType'
  | 'startEditingTransaction'
  | 'submitTransaction'
  | 'summary'
  | 'transactionType'
  | 'transactions'
  | 'valuation'
>;

export function ActivityPage({ workspace }: { workspace: Props }) {
  const {
    currency,
    loadingPortfolio,
    marketDataStatus,
    summary,
    transactions,
    valuation,
  } = workspace;

  return (
    <>
      <PortfolioSummary {...workspace} showRisk={false} />
      {!loadingPortfolio && summary && <div className="content-grid">
        <section className="data-section">
          <div className="section-heading"><div><p className="eyebrow">LEDGER</p><h2>Transaction history</h2></div><span className="section-count">{transactions.length} records</span></div>
          {transactions.length === 0 ? <div className="table-empty">No transactions recorded.</div> : (
            <div className="table-scroll"><table><thead><tr><th>Type</th><th>Instrument</th><th>Quantity</th><th>Gross amount</th><th>Fee</th><th>Event time</th><th /></tr></thead><tbody>{[...transactions].reverse().map((transaction) => <tr key={transaction.id}><td><span className={`type-label type-${transaction.transaction_type}`}>{transaction.transaction_type}</span></td><td>{transaction.symbol ?? 'Cash'}</td><td>{transaction.quantity ? formatQuantity(transaction.quantity) : '—'}</td><td>{formatMoney(transaction.amount, currency)}</td><td>{formatMoney(transaction.fee, currency)}</td><td>{new Date(transaction.occurred_at).toLocaleString()}</td><td><button className="icon-button" type="button" onClick={() => workspace.startEditingTransaction(transaction)} aria-label={`Edit ${transaction.transaction_type} transaction`}><Pencil size={15} /></button><button className="icon-button" type="button" onClick={() => workspace.removeTransaction(transaction.id)} aria-label={`Delete ${transaction.transaction_type} transaction`}><Trash2 size={15} /></button></td></tr>)}</tbody></table></div>
          )}
          <div className="valuation-footnote"><span className={`status-dot ${marketDataStatus}`} /> {valuation?.confidence === 'unreliable' ? `Valuation is unreliable: ${valuation.reasons.join(' ')}` : `Open cost basis ${formatMoney(summary.invested_cost, currency)} · market prices are not FX-converted.`}</div>
        </section>
        <TransactionEntry {...workspace} />
      </div>}
    </>
  );
}
