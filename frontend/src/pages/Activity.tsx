import { useEffect, useState } from 'react';
import { apiRequest } from '../api';
import { formatMoney, formatQuantity } from '../components/useWorkspace';

type DhanTrade = {
  id: string;
  trade_id: string;
  exchange_order_id: string | null;
  order_id: string | null;
  symbol: string | null;
  security_id: string | null;
  exchange_segment: string | null;
  side: string;
  quantity: string;
  price: string;
  charges: string | null;
  trade_at: string;
  broker: string;
  synced_at: string;
};

type DhanLedgerEntry = {
  id: string;
  entry_id: string;
  entry_date: string;
  description: string | null;
  debit: string | null;
  credit: string | null;
  running_balance: string | null;
  currency: string;
  broker: string;
  synced_at: string;
};

type DhanTransactionMirror = {
  currency: string;
  trades: DhanTrade[];
  ledger: DhanLedgerEntry[];
};

export function DhanTradeAndCashHistory() {
  const [brokerHistory, setBrokerHistory] = useState<DhanTransactionMirror | null>(null);
  const [brokerHistoryError, setBrokerHistoryError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    void apiRequest<DhanTransactionMirror>('/brokers/dhan/transactions')
      .then((result) => {
        if (active) setBrokerHistory(result);
      })
      .catch((error: unknown) => {
        if (active) {
          setBrokerHistoryError(
            error instanceof Error ? error.message : 'Could not load Dhan transaction history.',
          );
        }
      });
    return () => {
      active = false;
    };
  }, []);

  return (
    <section className="data-section">
      <div className="section-heading"><div><p className="eyebrow">READ-ONLY BROKER MIRROR</p><h2>Dhan trade and cash history</h2></div><span className="section-count">{(brokerHistory?.trades.length ?? 0) + (brokerHistory?.ledger.length ?? 0)} records</span></div>
      {brokerHistoryError && <p className="error-message" role="alert">{brokerHistoryError}</p>}
      {!brokerHistoryError && !brokerHistory && <div className="table-empty">Loading broker history…</div>}
      {brokerHistory && brokerHistory.trades.length + brokerHistory.ledger.length === 0 && <div className="table-empty">No mirrored Dhan trades or cash entries yet. Sync Dhan from the Brokers page.</div>}
      {brokerHistory && brokerHistory.trades.length + brokerHistory.ledger.length > 0 && (
        <div className="table-scroll"><table><thead><tr><th>Type</th><th>Instrument / description</th><th>Quantity</th><th>Price / cash movement</th><th>Event time</th><th>Broker</th><th>Synced</th></tr></thead><tbody>
          {brokerHistory.trades.map((trade) => <tr key={`trade-${trade.id}`}><td><span className={`type-label type-${trade.side.toLowerCase()}`}>{trade.side}</span></td><td>{trade.symbol ?? trade.security_id ?? 'Trade'}{trade.exchange_segment ? ` · ${trade.exchange_segment}` : ''}{trade.order_id ? ` · Order ${trade.order_id}` : ''}</td><td>{formatQuantity(trade.quantity)}</td><td>{formatMoney(trade.price, brokerHistory.currency)}{trade.charges != null ? ` · fees ${formatMoney(trade.charges, brokerHistory.currency)}` : ''}</td><td>{new Date(trade.trade_at).toLocaleString()}</td><td>{trade.broker}</td><td>{new Date(trade.synced_at).toLocaleString()}</td></tr>)}
          {brokerHistory.ledger.map((entry) => <tr key={`ledger-${entry.id}`}><td>Cash ledger</td><td>{entry.description ?? 'Cash movement'}</td><td>—</td><td>{entry.debit != null ? `Debit ${formatMoney(entry.debit, entry.currency)}` : ''}{entry.debit != null && entry.credit != null ? ' · ' : ''}{entry.credit != null ? `Credit ${formatMoney(entry.credit, entry.currency)}` : ''}{entry.running_balance != null ? ` · Balance ${formatMoney(entry.running_balance, entry.currency)}` : ''}</td><td>{new Date(entry.entry_date).toLocaleString()}</td><td>{entry.broker}</td><td>{new Date(entry.synced_at).toLocaleString()}</td></tr>)}
        </tbody></table></div>
      )}
    </section>
  );
}
