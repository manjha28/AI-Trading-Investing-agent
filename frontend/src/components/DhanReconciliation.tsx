import { useEffect, useState } from 'react';
import { apiRequest } from '../api';
import { Banner, Chip, ErrorState, Skeleton } from './ui/kit';
import { formatINR } from '../lib/format';

type Recon = {
  day: string; trades_matched_that_day: number; dhan: { realised: string | null; basis: string; snapshot_at: string | null };
  journal: { gross: string; fees_and_taxes_real: string; fees_and_taxes_estimated: string; net: string; trades_with_estimated_charges: number; trades_with_unknown_charges: number };
  unexplained: string | null; reconciles_within_1_rupee: boolean | null;
  by_contract: { symbol: string; journal_gross: string; dhan_realised: string | null; difference: string | null }[];
  open_lot_differences: { security_id: string; journal_net_quantity: string; dhan_net_quantity: string }[];
  contracts_without_master: { symbol: string; security_id: string; first_trade: string; reason: string }[]; notes: string[];
};

const row = (label: string, value: string | null, hint?: string) => <tr><th scope="row">{label}{hint && <small className="ui-muted"> {hint}</small>}</th><td className="ui-num">{value === null ? '—' : formatINR(value)}</td></tr>;

/** Dhan "Closed today" against the journal's net for the same day: gross, fees and taxes, and what is left unexplained. */
export function DhanReconciliation({ day }: { day?: string }) {
  const [data, setData] = useState<Recon | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [tick, setTick] = useState(0);
  useEffect(() => { setError(null); apiRequest<Recon>(`/brokers/dhan/reconciliation${day ? `?day=${day}` : ''}`).then(setData).catch((e: unknown) => setError(e)); }, [day, tick]);
  if (error) return <ErrorState error={error} onRetry={() => setTick((n) => n + 1)} context="Dhan reconciliation" />;
  if (!data) return <Skeleton variant="card" />;
  const est = Number(data.journal.fees_and_taxes_estimated);
  return (
    <section className="recon" aria-label="Dhan reconciliation">
      <h3>Reconciliation · {data.day} <Chip tone={data.reconciles_within_1_rupee === null ? 'neutral' : data.reconciles_within_1_rupee ? 'positive' : 'caution'}>{data.reconciles_within_1_rupee === null ? 'Dhan realised unknown' : data.reconciles_within_1_rupee ? 'reconciles' : 'unexplained gap'}</Chip></h3>
      <table className="recon-table"><tbody>
        {row('Dhan realised (gross)', data.dhan.realised, '· before brokerage, STT, exchange charges, GST and stamp duty')}
        {row(`Journal gross (${data.trades_matched_that_day} matched trades)`, data.journal.gross)}
        {row('Fees and taxes · real', data.journal.fees_and_taxes_real)}
        {row('Fees and taxes · estimated', data.journal.fees_and_taxes_estimated, `· ${data.journal.trades_with_estimated_charges} trades, app fee schedule, unverified rates`)}
        {row('Journal net', data.journal.net)}
        {row('Unexplained (Dhan realised − journal gross)', data.unexplained)}
      </tbody></table>
      {est > 0 && <Banner tone="caution">Part of the gap between Dhan&apos;s figure and the journal net is estimated charges. Check them against the Dhan contract note.</Banner>}
      {data.journal.trades_with_unknown_charges > 0 && <p className="ui-muted">{data.journal.trades_with_unknown_charges} matched trades have no charge figure and are left out of the net.</p>}
      {data.open_lot_differences.length > 0 && (
        <details><summary>Open lots differ from Dhan ({data.open_lot_differences.length})</summary>
          <ul>{data.open_lot_differences.map((d) => <li key={d.security_id}>Contract {d.security_id}: journal net {d.journal_net_quantity}, Dhan {d.dhan_net_quantity} (fills before the sync window or a stale positions snapshot)</li>)}</ul></details>
      )}
      {data.contracts_without_master.length > 0 && (
        <details><summary>Contracts missing from the Dhan master ({data.contracts_without_master.length})</summary>
          <ul>{data.contracts_without_master.map((c) => <li key={c.security_id}><strong>{c.symbol}</strong> · first traded {c.first_trade}: {c.reason}</li>)}</ul></details>
      )}
      <ul className="ui-muted">{data.notes.map((n) => <li key={n}>{n}</li>)}</ul>
    </section>
  );
}
