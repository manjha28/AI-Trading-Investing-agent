import { useCallback, useEffect, useState, type FormEvent } from 'react';
import { ApiRequestError, apiRequest } from '../api';
import '../brokers.css';

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
type AngelOneMarketSource = {
  available: boolean;
  status: string;
  expires_at: string | null;
};
type AngelOneMarketSettings = {
  sources: { name: string; label: string; available: boolean; status: string; expires_at?: string | null }[];
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

type SyncResult = {
  sync_status: 'successful' | 'partial';
  authentication_status: 'connected' | 'expired';
  endpoint_results: Record<string, { status: 'succeeded' | 'failed'; reason?: string }>;
  holdings_count: number;
  positions_count: number;
  synthetic: boolean;
};

function brokerErrorMessage(error: unknown): string {
  if (!(error instanceof ApiRequestError)) return (error as Error).message;
  switch (error.code) {
    case 'email_verification_required':
      return 'Verify your email before accessing broker connections.';
    case 'two_factor_setup_required':
      return 'Set up app-based two-factor authentication before accessing broker connections.';
    case 'two_factor_required':
      return 'Verify this session with your authenticator app before accessing broker connections.';
    case 'broker_setup_required':
      return `Broker setup is unavailable: ${error.message}`;
    case 'broker_authentication_required':
      return `Broker sign-in is required again: ${error.message}`;
    case 'broker_provider_error':
      return `The broker provider could not complete the request: ${error.message}`;
    default:
      return error.message;
  }
}

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
  const [syncResults, setSyncResults] = useState<Record<string, SyncResult>>({});
  const [syncErrors, setSyncErrors] = useState<Record<string, string>>({});
  const [clientId, setClientId] = useState('');
  const [busy, setBusy] = useState('');
  const [failedBroker, setFailedBroker] = useState('');
  const [error, setError] = useState('');
  const [brokerApiUnavailable, setBrokerApiUnavailable] = useState(false);
  const [notice, setNotice] = useState('');
  const [angelOneSource, setAngelOneSource] = useState<AngelOneMarketSource | null>(null);
  const [angelOneClientCode, setAngelOneClientCode] = useState('');
  const [angelOnePin, setAngelOnePin] = useState('');
  const [angelOneTotp, setAngelOneTotp] = useState('');
  const [angelOneBusy, setAngelOneBusy] = useState(false);
  const [angelOneError, setAngelOneError] = useState('');
  const [angelOneReconnectMode, setAngelOneReconnectMode] = useState(false);

  const refreshAngelOneStatus = useCallback(async () => {
    const settings = await apiRequest<AngelOneMarketSettings>('/market-data/settings');
    const source = settings.sources.find((item) => item.name === 'angel_one');
    setAngelOneSource(source ? {
      available: source.available,
      status: source.status,
      expires_at: source.expires_at ?? null,
    } : null);
  }, []);

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
          return [broker.slug, null, brokerErrorMessage(requestError)] as const;
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
      setError(brokerErrorMessage(requestError));
    });
  }, [refresh]);

  useEffect(() => {
    void refreshAngelOneStatus().catch((requestError: Error) => {
      setAngelOneError(brokerErrorMessage(requestError));
    });
  }, [refreshAngelOneStatus]);

  async function connectAngelOne(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setAngelOneBusy(true);
    setAngelOneError('');
    setError('');
    setNotice('');
    try {
      await apiRequest('/market-data/settings/angel-one/connect', {
        method: 'POST',
        body: JSON.stringify({
          client_code: angelOneClientCode,
          pin: angelOnePin,
          totp: angelOneTotp,
        }),
      });
      setAngelOnePin('');
      setAngelOneTotp('');
      await refreshAngelOneStatus();
      setAngelOneReconnectMode(false);
      setNotice('Angel One SmartAPI connected for market data. Credentials remain backend-only.');
    } catch (requestError) {
      setAngelOneError(brokerErrorMessage(requestError));
    } finally {
      setAngelOnePin('');
      setAngelOneTotp('');
      setAngelOneBusy(false);
    }
  }

  async function disconnectAngelOne() {
    if (!window.confirm('Disconnect Angel One SmartAPI market-data access?')) return;
    setAngelOneBusy(true);
    setAngelOneError('');
    try {
      await apiRequest('/market-data/settings/angel-one/disconnect', {
        method: 'POST',
        body: '{}',
      });
      await refreshAngelOneStatus();
      setNotice('Angel One SmartAPI disconnected.');
    } catch (requestError) {
      setAngelOneError(brokerErrorMessage(requestError));
    } finally {
      setAngelOneBusy(false);
    }
  }

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
      setError(brokerErrorMessage(requestError));
    } finally {
      setBusy('');
    }
  }

  async function sync(broker: Broker) {
    setError('');
    setNotice('');
    setFailedBroker('');
    setSyncErrors((current) => ({ ...current, [broker.slug]: '' }));
    setBusy(broker.slug);
    try {
      const result = await apiRequest<SyncResult>(
        `/brokers/${broker.slug}/sync`,
        { method: 'POST', body: '{}' },
      );
      setSyncResults((current) => ({ ...current, [broker.slug]: result }));
      await refresh();
      if (result.sync_status === 'successful') {
        setNotice(
          `${broker.name}: synced ${result.holdings_count} holding(s) and ${result.positions_count} position(s).${result.synthetic ? ' These values are synthetic demo data.' : ''}`,
        );
      }
    } catch (requestError) {
      setSyncErrors((current) => ({
        ...current,
        [broker.slug]: brokerErrorMessage(requestError),
      }));
      if (requestError instanceof ApiRequestError && requestError.code === 'broker_authentication_required') {
        void refresh().catch((refreshError: Error) => setError(brokerErrorMessage(refreshError)));
      }
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
      setError(brokerErrorMessage(requestError));
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
      setError(brokerErrorMessage(requestError));
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
        <article className="broker-card">
          <div className="broker-card-heading">
            <span className="broker-logo" aria-hidden="true">AO</span>
            <div>
              <h2>Angel One SmartAPI</h2>
              <p className={angelOneSource?.available ? 'broker-status connected' : 'broker-status'}>
                {angelOneSource?.status === 'session_expired'
                  ? 'Session expired — reconnect required'
                  : angelOneSource?.available
                    ? 'Connected for market data'
                    : angelOneSource?.status === 'provider_configuration_required'
                      ? 'Backend setup required'
                      : 'Not connected'}
              </p>
            </div>
          </div>
          {angelOneSource?.expires_at && (
            <p className="broker-sync-time">
              Session expiry: {new Date(angelOneSource.expires_at).toLocaleString()}
            </p>
          )}
          {(
            !angelOneSource?.available
            || angelOneSource.status === 'session_expired'
            || angelOneReconnectMode
          ) && (
            <form className="broker-form" onSubmit={(event) => void connectAngelOne(event)}>
              <label className="broker-secret-field">
                Client code
                <input value={angelOneClientCode} onChange={(event) => setAngelOneClientCode(event.target.value)} autoComplete="username" required />
              </label>
              <label className="broker-secret-field">
                PIN
                <input type="password" value={angelOnePin} onChange={(event) => setAngelOnePin(event.target.value)} autoComplete="current-password" required />
              </label>
              <label className="broker-secret-field">
                Authenticator code
                <input inputMode="numeric" pattern="[0-9]{6}" maxLength={6} value={angelOneTotp} onChange={(event) => setAngelOneTotp(event.target.value)} autoComplete="one-time-code" required />
              </label>
              <button className="primary-button" type="submit" disabled={angelOneBusy}>
                {angelOneBusy ? 'Connecting…' : angelOneSource?.status === 'session_expired' ? 'Reconnect' : 'Connect'}
              </button>
            </form>
          )}
          {angelOneSource?.available
            && angelOneSource.status !== 'session_expired'
            && !angelOneReconnectMode && (
            <div className="broker-actions">
              <button className="secondary-button" type="button" onClick={() => void disconnectAngelOne()} disabled={angelOneBusy}>Disconnect</button>
              <button className="secondary-button" type="button" onClick={() => setAngelOneReconnectMode(true)} disabled={angelOneBusy}>Reconnect</button>
            </div>
          )}
          {angelOneError && <p className="broker-sync-time" role="alert">{angelOneError}</p>}
        </article>
        {brokers.filter((broker) => broker.slug !== 'angel_one').map((broker) => (
          <article className="broker-card" key={broker.slug}>
            <div className="broker-card-heading">
              <span className="broker-logo" aria-hidden="true">{broker.name.split(/\s+/).map((part) => part[0]).join('').slice(0, 2)}</span>
              <div>
                <h2>{broker.name}</h2>
                <p className={broker.connected ? 'broker-status connected' : 'broker-status'}>
                      {busy === broker.slug
                            ? broker.connected ? 'Syncing…' : 'Connecting…'
                        : broker.connection_status === 'connected'
                        ? 'Connected'
                        : broker.connection_status === 'expired'
                        ? 'Connection expired — reconnect required'
                        : failedBroker === broker.slug
                          ? 'Connection attempt failed'
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
            {syncResults[broker.slug] && (
              <p className="broker-sync-time" role="status">
                Account sync: {syncResults[broker.slug].sync_status}. {Object.entries(syncResults[broker.slug].endpoint_results)
                  .map(([endpoint, result]) => `${endpoint}: ${result.status}`).join(' · ')}
              </p>
            )}
            {syncErrors[broker.slug] && <p className="broker-sync-time" role="alert">Account sync failed: {syncErrors[broker.slug]} Authentication status is shown separately above. Retry with Sync.</p>}
            <p className="broker-sync-time">Market data is separate from account sync; check the Markets page for quote freshness and feed status.</p>
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
