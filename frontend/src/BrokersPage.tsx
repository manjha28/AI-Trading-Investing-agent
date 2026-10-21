import { useCallback, useEffect, useState } from 'react';
import { ApiRequestError, apiRequest } from './api';
import './brokers.css';

type Broker = {
  slug: string;
  name: string;
  enabled: boolean;
  synthetic: boolean;
  connected: boolean;
  expires_at: string | null;
  last_synced_at: string | null;
  direct_login: boolean;
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

const readableStatus: Record<Difference['status'], string> = {
  missing: 'Missing at broker',
  extra: 'Not in manual ledger',
  quantity_mismatch: 'Quantity mismatch',
};

export function BrokersPage({ portfolioId }: { portfolioId: string }) {
  const [brokers, setBrokers] = useState<Broker[]>([]);
  const [differences, setDifferences] = useState<Record<string, Difference[]>>({});
  const [clientCode, setClientCode] = useState('');
  const [pin, setPin] = useState('');
  const [totp, setTotp] = useState('');
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [brokerApiUnavailable, setBrokerApiUnavailable] = useState(false);
  const [notice, setNotice] = useState('');

  const refresh = useCallback(async () => {
    const nextBrokers = await apiRequest<Broker[]>('/brokers');
    setBrokers(nextBrokers);
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
    setBusy(broker.slug);
    try {
      const body = broker.slug === 'angel_one'
        ? { client_code: clientCode, pin, totp }
        : broker.slug === 'dhan'
          ? { client_id: clientCode }
          : {};
      const result = await apiRequest<{ redirect_url: string | null; connected: boolean }>(
        `/brokers/${broker.slug}/connect`,
        { method: 'POST', body: JSON.stringify(body) },
      );
      setPin('');
      setTotp('');
      if (result.redirect_url) {
        window.location.assign(result.redirect_url);
        return;
      }
      await refresh();
      setNotice(`${broker.name} connected. No orders can be placed through this connection.`);
    } catch (requestError) {
      setError((requestError as Error).message);
    } finally {
      setBusy('');
    }
  }

  async function sync(broker: Broker) {
    setError('');
    setNotice('');
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
      setError((requestError as Error).message);
    } finally {
      setBusy('');
    }
  }

  async function disconnect(broker: Broker) {
    if (!window.confirm(`Disconnect ${broker.name}? This deletes its saved connection credential.`)) return;
    setError('');
    setNotice('');
    setBusy(broker.slug);
    try {
      await apiRequest<void>(`/brokers/${broker.slug}`, { method: 'DELETE' });
      await refresh();
      setNotice(`${broker.name} disconnected. Its previously synced mirror remains read-only.`);
    } catch (requestError) {
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
                  {broker.connected ? 'Connected' : broker.enabled ? 'Not connected' : 'Disabled — credentials required'}
                </p>
              </div>
            </div>
            {broker.synthetic && <p className="broker-synthetic">SYNTHETIC DEMO DATA — NOT A REAL ACCOUNT</p>}
            {broker.last_synced_at && <p className="broker-sync-time">Last sync: {new Date(broker.last_synced_at).toLocaleString()}</p>}
            {broker.expires_at && <p className="broker-sync-time">Session expiry: {new Date(broker.expires_at).toLocaleString()}</p>}
            {broker.slug === 'dhan' && !broker.connected && broker.enabled && (
              <label className="broker-secret-field">Dhan client ID<input value={clientCode} onChange={(event) => setClientCode(event.target.value)} autoComplete="off" /></label>
            )}
            {broker.slug === 'angel_one' && !broker.connected && broker.enabled && (
              <div className="broker-login-fields">
                <label className="broker-secret-field">Client code<input value={clientCode} onChange={(event) => setClientCode(event.target.value)} autoComplete="off" /></label>
                <label className="broker-secret-field">PIN<input type="password" value={pin} onChange={(event) => setPin(event.target.value)} autoComplete="new-password" /></label>
                <label className="broker-secret-field">TOTP<input type="password" value={totp} onChange={(event) => setTotp(event.target.value)} autoComplete="one-time-code" /></label>
                <small>Sent only to SmartAPI for login and never stored.</small>
              </div>
            )}
            <div className="broker-actions">
              {!broker.connected ? (
                <button className="primary-button" type="button" onClick={() => void connect(broker)} disabled={!broker.enabled || busy === broker.slug || (broker.slug === 'dhan' && !clientCode) || (broker.slug === 'angel_one' && (!clientCode || !pin || !totp))}>
                  {busy === broker.slug ? 'Connecting…' : 'Connect'}
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
      </div>}
      {!brokerApiUnavailable && !brokers.length && !error && <p className="table-empty">Loading available brokers…</p>}
      {!portfolioId && <p className="accounting-note">Create or select a manual portfolio to compare holdings and import confirmed differences.</p>}
    </section>
  );
}
