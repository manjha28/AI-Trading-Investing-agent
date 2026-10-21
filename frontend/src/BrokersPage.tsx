import { useCallback, useEffect, useState } from 'react';
import { ApiRequestError, apiRequest } from './api';
import './brokers.css';

type Broker = {
  slug: string;
  name: string;
  enabled: boolean;
  synthetic: boolean;
  connected: boolean;
  connection_status: 'connected' | 'expired' | 'not_connected';
  integration_status: 'available' | 'setup_required' | 'pending' | 'synthetic';
  expires_at: string | null;
  last_synced_at: string | null;
};

type Difference = {
  symbol: string;
  manual_quantity: string;
  broker_quantity: string;
  difference: string;
  status: 'missing' | 'extra' | 'quantity_mismatch';
  average_price: string | null;
  exchanges: string[];
};

type BrokerAccount = {
  profile: Record<string, unknown>;
  funds: unknown;
  order_history: unknown[];
  synthetic: boolean;
};

const readableStatus: Record<Difference['status'], string> = {
  missing: 'Missing at broker',
  extra: 'Not in manual ledger',
  quantity_mismatch: 'Quantity mismatch',
};

export function BrokersPage({ portfolioId }: { portfolioId: string }) {
  const [brokers, setBrokers] = useState<Broker[]>([]);
  const [differences, setDifferences] = useState<Record<string, Difference[]>>({});
  const [accounts, setAccounts] = useState<Record<string, BrokerAccount>>({});
  const [accountErrors, setAccountErrors] = useState<Record<string, string>>({});
  const [clientId, setClientId] = useState('');
  const [busy, setBusy] = useState('');
  const [failedBroker, setFailedBroker] = useState('');
  const [error, setError] = useState('');
  const [brokerApiUnavailable, setBrokerApiUnavailable] = useState(false);
  const [notice, setNotice] = useState('');

  const refresh = useCallback(async () => {
    const nextBrokers = await apiRequest<Broker[]>('/brokers');
    setBrokers(nextBrokers);
    const accountResults = await Promise.all(nextBrokers
      .filter((broker) => broker.connected)
      .map(async (broker) => {
        try {
          const account = await apiRequest<BrokerAccount>(`/brokers/${broker.slug}/account`);
          return [broker.slug, account, ''] as const;
        } catch (requestError) {
          return [broker.slug, null, (requestError as Error).message] as const;
        }
      }));
    const nextAccounts: Record<string, BrokerAccount> = {};
    const nextAccountErrors: Record<string, string> = {};
    for (const [slug, account, message] of accountResults) {
      if (account) nextAccounts[slug] = account;
      else if (message) nextAccountErrors[slug] = message;
    }
    setAccounts(nextAccounts);
    setAccountErrors(nextAccountErrors);
    if (!portfolioId) {
      setDifferences({});
      return;
    }
    const rows = await Promise.all(nextBrokers
      .filter((broker) => broker.connected && broker.last_synced_at)
      .map(async (broker) => {
        const result = await apiRequest<{ differences: Difference[] }>(
          `/brokers/${broker.slug}/reconciliation?portfolio_id=${encodeURIComponent(portfolioId)}`,
        );
        return [broker.slug, result.differences] as const;
      }));
    setDifferences(Object.fromEntries(rows));
  }, [portfolioId]);

  useEffect(() => {
    void refresh().catch((requestError: Error) => {
      if (requestError instanceof ApiRequestError && requestError.status === 404) {
        setBrokerApiUnavailable(true);
        return;
      }
      setError(requestError.message);
    });
  }, [refresh]);

  async function connect(broker: Broker) {
    setError('');
    setNotice('');
    setFailedBroker('');
    setBusy(broker.slug);
    try {
      const body = broker.slug === 'dhan' ? { client_id: clientId } : {};
      const result = await apiRequest<{ redirect_url: string | null; connected: boolean }>(
        `/brokers/${broker.slug}/connect`,
        { method: 'POST', body: JSON.stringify(body) },
      );
      if (result.redirect_url) {
        window.location.assign(result.redirect_url);
        return;
      }
      await refresh();
      setNotice(`${broker.name} connected. No orders can be placed through this connection.`);
    } catch (requestError) {
      setFailedBroker(broker.slug);
      setError((requestError as Error).message);
    } finally {
      setBusy('');
    }
  }

  async function sync(broker: Broker) {
    setError('');
    setNotice('');
    setFailedBroker('');
    setBusy(broker.slug);
    try {
      const result = await apiRequest<{ holdings_count: number; positions_count: number; synthetic: boolean }>(
        `/brokers/${broker.slug}/sync`,
        { method: 'POST', body: '{}' },
      );
      await refresh();
      setNotice(
        `${broker.name}: synced ${result.holdings_count} holding(s) and ${result.positions_count} position(s).${result.synthetic ? ' These values are synthetic demo data.' : ''}`,
      );
    } catch (requestError) {
      setFailedBroker(broker.slug);
      setError((requestError as Error).message);
    } finally {
      setBusy('');
    }
  }

  async function disconnect(broker: Broker) {
    if (!window.confirm(`Disconnect ${broker.name}? This deletes its saved connection credential.`)) return;
    setError('');
    setNotice('');
    setFailedBroker('');
    setBusy(broker.slug);
    try {
      await apiRequest<void>(`/brokers/${broker.slug}`, { method: 'DELETE' });
      await refresh();
      setNotice(`${broker.name} disconnected. Its previously synced mirror remains read-only.`);
    } catch (requestError) {
      setFailedBroker(broker.slug);
      setError((requestError as Error).message);
    } finally {
      setBusy('');
    }
  }

  async function importDifference(broker: Broker, difference: Difference) {
    if (!portfolioId) {
      setError('Choose a manual portfolio before importing a reconciliation difference.');
      return;
    }
    const quantity = Number(difference.difference);
    const action = quantity > 0 ? 'buy' : 'sell';
    const confirmed = window.confirm(
      `Create a manual ${action} transaction for ${Math.abs(quantity)} ${difference.symbol} at the broker average price? This only changes your manual ledger.`,
    );
    if (!confirmed) return;
    setError('');
    setNotice('');
    setBusy(`${broker.slug}:${difference.symbol}`);
    try {
      const result = await apiRequest<{ imported_count: number }>(
        `/brokers/${broker.slug}/reconciliation/import`,
        {
          method: 'POST',
          body: JSON.stringify({
            portfolio_id: portfolioId,
            confirmed: true,
            rows: [{
              symbol: difference.symbol,
              expected_manual_quantity: difference.manual_quantity,
              expected_broker_quantity: difference.broker_quantity,
            }],
          }),
        },
      );
      await refresh();
      setNotice(`${result.imported_count} explicitly confirmed manual transaction imported.`);
    } catch (requestError) {
      setError((requestError as Error).message);
    } finally {
      setBusy('');
    }
  }

  return (
    <section className="brokers-page">
      <div className="broker-disclaimer" role="note">
        <strong>Read-only broker access.</strong> Trading Copilot only reads your profile, holdings, positions,
        order history, and funds. It has no code path to place, modify, or cancel broker orders. Broker data
        stays in a separate mirror and never overwrites your manual portfolio. Any reconciliation import
        creates a manual ledger transaction only after you confirm it.
      </div>

      {error && <p className="error-banner" role="alert">{error}</p>}
      {notice && <p className="success-banner" role="status">{notice}</p>}
      {brokerApiUnavailable && (
        <div className="broker-disclaimer" role="status">
          <strong>Broker integration unavailable.</strong> This backend does not expose the broker API.
          No broker connection was attempted. Restart or update the backend, then reload this page.
        </div>
      )}

      {!brokerApiUnavailable && <div className="broker-card-grid">
        {brokers.map((broker) => (
          <article className="broker-card" key={broker.slug}>
            <div className="broker-card-heading">
              <span className="broker-logo" aria-hidden="true">{broker.name.split(/\s+/).map((part) => part[0]).join('').slice(0, 2)}</span>
              <div>
                <h2>{broker.name}</h2>
                <p className={broker.connected ? 'broker-status connected' : 'broker-status'}>
                      {busy === broker.slug
                            ? broker.connected ? 'Syncing…' : 'Connecting…'
                        : failedBroker === broker.slug
                          ? 'Connection failed'
                          : broker.connection_status === 'connected'
                        ? 'Connected'
                        : broker.connection_status === 'expired'
                          ? 'Connection expired — reconnect required'
                          : broker.integration_status === 'pending'
                            ? 'Pending — official redirect flow required'
                            : broker.enabled
                              ? 'Not connected'
                              : broker.integration_status === 'synthetic'
                                ? 'Synthetic demo only'
                                : 'Not connected — app setup required'}
                    </p>
              </div>
            </div>
            {broker.synthetic && <p className="broker-synthetic">SYNTHETIC DEMO DATA — NOT A REAL ACCOUNT</p>}
            {broker.last_synced_at && <p className="broker-sync-time">Last sync: {new Date(broker.last_synced_at).toLocaleString()}</p>}
            {broker.expires_at && <p className="broker-sync-time">Session expiry: {new Date(broker.expires_at).toLocaleString()}</p>}
            {broker.last_synced_at && (
              <p className="broker-sync-time">
                Account data source: {broker.synthetic ? 'synthetic demo' : 'live broker API'} · Last successful sync {new Date(broker.last_synced_at).toLocaleString()}
              </p>
            )}
            {broker.slug === 'dhan' && !broker.connected && broker.enabled && broker.integration_status === 'available' && (
              <label className="broker-secret-field">Dhan client ID<input value={clientId} onChange={(event) => setClientId(event.target.value)} autoComplete="off" /></label>
            )}
            {broker.integration_status === 'pending' && (
              <p className="broker-sync-time">Authentication is not available yet. Trading Copilot never collects your broker password, PIN, or OTP.</p>
            )}
            {broker.integration_status === 'setup_required' && (
              <p className="broker-sync-time">Requires a developer app, registered redirect URL, and server-side API credentials.</p>
            )}
            {accountErrors[broker.slug] && <p className="broker-sync-time">Account details unavailable: {accountErrors[broker.slug]}</p>}
            {accounts[broker.slug] && (
              <details className="broker-sync-time">
                <summary>{accounts[broker.slug].synthetic ? 'Synthetic demo account details' : 'Live broker account details'}</summary>
                <p>Profile: {JSON.stringify(accounts[broker.slug].profile)}</p>
                <p>Funds: {JSON.stringify(accounts[broker.slug].funds)}</p>
                <p>Read-only order history: {accounts[broker.slug].order_history.length} record(s)</p>
              </details>
            )}
            <div className="broker-actions">
              {!broker.connected ? (
                <button className="primary-button" type="button" onClick={() => void connect(broker)} disabled={!broker.enabled || busy === broker.slug || (broker.slug === 'dhan' && !clientId)}>
                  {busy === broker.slug ? 'Connecting…' : broker.connection_status === 'expired' ? 'Reconnect' : 'Connect'}
                </button>
              ) : (
                <>
                  <button className="primary-button" type="button" onClick={() => void sync(broker)} disabled={busy === broker.slug}>
                    {busy === broker.slug ? 'Syncing…' : 'Sync'}
                  </button>
                  <button className="secondary-button" type="button" onClick={() => void disconnect(broker)} disabled={busy === broker.slug}>Disconnect</button>
                </>
              )}
            </div>
            {broker.connected && broker.last_synced_at && portfolioId && (
              <section className="broker-reconciliation">
                <div className="section-heading">
                  <div><p className="eyebrow">READ-ONLY MIRROR</p><h3>Reconciliation</h3></div>
                  <span className="section-count">{differences[broker.slug]?.length ?? 0} differences</span>
                </div>
                {!differences[broker.slug]?.length ? (
                  <p className="table-empty">No differences found, or reconciliation is still loading.</p>
                ) : (
                  <div className="broker-table-wrap">
                    <table className="broker-table">
                      <thead><tr><th>Symbol</th><th>Manual</th><th>Broker</th><th>Difference</th><th>Status</th><th /></tr></thead>
                      <tbody>
                        {differences[broker.slug].map((difference) => (
                          <tr key={difference.symbol}>
                            <td><strong>{difference.symbol}</strong><small>{difference.exchanges.join(', ')}</small></td>
                            <td>{difference.manual_quantity}</td>
                            <td>{difference.broker_quantity}</td>
                            <td>{difference.difference}</td>
                            <td>{readableStatus[difference.status]}</td>
                            <td>
                              {difference.status !== 'missing' && difference.average_price && Number(difference.average_price) > 0 ? (
                                <button className="secondary-button" type="button" onClick={() => void importDifference(broker, difference)} disabled={Boolean(busy)}>
                                  Import difference
                                </button>
                              ) : difference.status !== 'missing' ? <span>No cost basis</span> : null}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </section>
            )}
          </article>
        ))}
        <article className="broker-card">
          <div className="broker-card-heading">
            <span className="broker-logo" aria-hidden="true">+</span>
            <div><h2>Other brokers</h2><p className="broker-status">Not supported yet</p></div>
          </div>
          <p className="broker-sync-time">Only the brokers listed here have adapters. No connection is attempted for other providers.</p>
          <button className="secondary-button" type="button" disabled>Pending integration</button>
        </article>
      </div>}
      {!brokerApiUnavailable && !brokers.length && !error && <p className="table-empty">Loading available brokers…</p>}
      {!portfolioId && <p className="accounting-note">Create or select a manual portfolio to compare holdings and import confirmed differences.</p>}
    </section>
  );
}
