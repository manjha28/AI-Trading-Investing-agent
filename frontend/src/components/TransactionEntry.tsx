import { ArrowUpRight, Plus, ShieldCheck } from 'lucide-react';
import { localDateTime, type TransactionType, type WorkspaceState } from './useWorkspace';

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
  | 'previewTransactionCsv'
  | 'setTransactionType'
  | 'submitTransaction'
  | 'transactionType'
>;

export function TransactionEntry({
  cancelEditingTransaction,
  csvContent,
  csvPreview,
  currency,
  editingTransaction,
  exportTransactionCsv,
  importTransactionCsv,
  importingCsv,
  loadTransactionCsv,
  previewTransactionCsv,
  setTransactionType,
  submitTransaction,
  transactionType,
}: Props) {
  return (
    <section className="entry-section">
      <div className="section-heading"><div><p className="eyebrow">{editingTransaction ? 'EDIT ENTRY' : 'MANUAL ENTRY'}</p><h2>{editingTransaction ? 'Edit activity' : 'Record activity'}</h2></div>{editingTransaction ? <button className="text-button" type="button" onClick={cancelEditingTransaction}>Cancel</button> : <Plus size={18} />}</div>
      <form key={editingTransaction?.id ?? 'new-transaction'} className="form-stack compact" onSubmit={submitTransaction}>
        <label>Activity type<select name="transaction_type" value={transactionType} onChange={(event) => setTransactionType(event.target.value as TransactionType)}><option value="deposit">Deposit</option><option value="withdrawal">Withdrawal</option><option value="dividend">Dividend</option><option value="buy">Buy equity</option><option value="sell">Sell equity</option></select></label>
        {['buy', 'sell', 'dividend'].includes(transactionType) && <label>Symbol<input name="symbol" placeholder="e.g. AAPL" maxLength={32} defaultValue={editingTransaction?.symbol ?? ''} required /></label>}
        {['buy', 'sell'].includes(transactionType) && <>
          <label>Quantity<input name="quantity" type="number" min="0.00000001" step="0.00000001" defaultValue={editingTransaction?.quantity ?? ''} required /></label>
          <label>Unit price<input name="unit_price" type="number" min="0.00000001" step="0.00000001" defaultValue={editingTransaction?.unit_price ?? ''} required /></label>
        </>}
        {!['buy', 'sell'].includes(transactionType) && <label>Gross amount<input name="amount" type="number" min="0.01" step="0.01" defaultValue={editingTransaction?.amount ?? ''} required /></label>}
        <label>Fee<input name="fee" type="number" min="0" step="0.01" defaultValue={editingTransaction?.fee ?? '0.00'} required /></label>
        <label>Event time<input name="occurred_at" type="datetime-local" defaultValue={localDateTime(editingTransaction?.occurred_at) || new Date(Date.now() - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 16)} required /></label>
        <button className="primary-button" type="submit"><ArrowUpRight size={17} /> {editingTransaction ? 'Update transaction' : 'Save transaction'}</button>
      </form>
      <div className="csv-tools">
        <p className="eyebrow">CSV</p>
        <button className="secondary-button" type="button" onClick={exportTransactionCsv}>Export transactions</button>
        <label className="csv-file">Import file<input type="file" accept=".csv,text/csv" onChange={(event) => void loadTransactionCsv(event.target.files?.[0])} /></label>
        <button className="secondary-button" type="button" onClick={previewTransactionCsv} disabled={!csvContent}>Preview import</button>
        {csvPreview && <>
          <p className="csv-summary">{csvPreview.valid_count} ready · {csvPreview.duplicate_count} duplicate · {csvPreview.invalid_count} invalid</p>
          {csvPreview.rows.some((row) => row.status !== 'valid') && <ul className="csv-preview" aria-label="CSV row validation results">
            {csvPreview.rows.filter((row) => row.status !== 'valid').slice(0, 8).map((row) => (
              <li key={row.row_number} className={row.status}>
                Row {row.row_number}: {row.status === 'duplicate' ? 'already recorded' : row.errors.join(', ')}
              </li>
            ))}
          </ul>}
          <button className="primary-button" type="button" onClick={importTransactionCsv} disabled={csvPreview.valid_count === 0 || csvPreview.invalid_count > 0 || importingCsv}>
            {importingCsv ? 'Importing…' : `Import ${csvPreview.valid_count} transaction${csvPreview.valid_count === 1 ? '' : 's'}`}
          </button>
        </>}
        <p className="accounting-note">Expected columns: transaction_type, symbol, quantity, unit_price, amount, fee, occurred_at. Preview is required; duplicate rows are skipped.</p>
      </div>
      <p className="accounting-note"><ShieldCheck size={15} /> Long-only cash equities. Fees and cost basis use {currency} minor units; no FX conversion.</p>
    </section>
  );
}
