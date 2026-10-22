import { useCallback, useEffect, useState, type FormEvent } from 'react';
import { apiRequest, ApiRequestError } from '../api';

type Action = {
  id: string;
  instrument_id: string;
  symbol: string;
  action_type: 'split' | 'bonus' | 'dividend';
  ex_date: string;
  ratio: string | null;
  amount: string | null;
};
type Application = {
  id: string;
  corporate_action_id: string;
  symbol: string;
  action_type: Action['action_type'];
  ex_date: string;
  ratio: string | null;
  amount: string | null;
  applied_at: string;
  reversed_at: string | null;
  affected_quantity: string;
  quantity_adjustment: string;
  cash_adjustment: string;
  active: boolean;
};
type Instrument = { id: string; symbol: string; display_name: string; venue: string; currency: string };

function guidance(error: unknown, operation: 'apply' | 'reverse'): string {
  const message = error instanceof ApiRequestError ? error.message : '';
  if (/link every transaction|link.*instrument/i.test(message)) {
    return 'Link every buy/sell transaction for this holding to the selected market instrument first. Open Market Data, select the instrument, and use “Link to portfolio holding.”';
  }
  if (operation === 'reverse' && /later trades|after later trades/i.test(message)) {
    return 'Later trades use post-action share units, so this split/bonus cannot be reversed yet. Correct those later transactions first.';
  }
  return message || 'The corporate action request could not be completed.';
}

export function CorporateActions({ portfolioId, watchlist, addInstrumentToWatchlist, removeInstrumentFromWatchlist }: {
  portfolioId: string;
  watchlist: { instrument_id: string; symbol: string }[];
  addInstrumentToWatchlist: (instrumentId: string, symbol: string) => Promise<void>;
  removeInstrumentFromWatchlist: (instrumentId: string) => Promise<void>;
}) {
  const [actions, setActions] = useState<Action[]>([]);
  const [applications, setApplications] = useState<Application[]>([]);
  const [instrumentChoices, setInstrumentChoices] = useState<Instrument[]>([]);
  const [instrumentQuery, setInstrumentQuery] = useState('');
  const [searchingInstruments, setSearchingInstruments] = useState(false);
  const [instrumentId, setInstrumentId] = useState('');
  const [actionType, setActionType] = useState<Action['action_type']>('split');
  const [exDate, setExDate] = useState('');
  const [ratio, setRatio] = useState('');
  const [amount, setAmount] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [rowErrors, setRowErrors] = useState<Record<string, string>>({});

  const refresh = useCallback(async () => {
    const [nextActions, nextApplications] = await Promise.all([
      apiRequest<Action[]>('/corporate-actions'),
      portfolioId
        ? apiRequest<Application[]>(`/corporate-actions/portfolios/${portfolioId}/applications`)
        : Promise.resolve([]),
    ]);
    setActions(nextActions);
    setApplications(nextApplications);
  }, [portfolioId]);

  useEffect(() => {
    void refresh().catch((requestError: Error) => setError(requestError.message));
  }, [refresh]);

  async function createAction(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError('');
    setNotice('');
    setBusy(true);
    try {
      const result = await apiRequest<Action>('/corporate-actions', {
        method: 'POST',
        body: JSON.stringify({
          instrument_id: instrumentId,
          action_type: actionType,
          ex_date: exDate,
          ...(actionType === 'dividend' ? { amount } : { ratio }),
        }),
      });
      setNotice(`${result.symbol} ${result.action_type} action created.`);
      setRatio('');
      setAmount('');
      await refresh();
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : 'Could not create corporate action.');
    } finally {
      setBusy(false);
    }
  }

  async function searchInstruments() {
    setSearchingInstruments(true);
    setError('');
    try {
      const query = new URLSearchParams({ query: instrumentQuery.trim(), limit: '30' });
      setInstrumentChoices(await apiRequest<Instrument[]>(`/market-data/instruments?${query}`));
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : 'Instrument search failed.');
    } finally {
      setSearchingInstruments(false);
    }
  }

  async function applyAction(action: Action) {
    setRowErrors((current) => ({ ...current, [action.id]: '' }));
    try {
      await apiRequest<Application>(`/corporate-actions/portfolios/${portfolioId}/${action.id}/apply`, { method: 'POST' });
      setNotice(`${action.symbol} action applied to the selected portfolio.`);
      await refresh();
    } catch (requestError) {
      setRowErrors((current) => ({ ...current, [action.id]: guidance(requestError, 'apply') }));
    }
  }

  async function reverseApplication(application: Application) {
    const changes = application.action_type === 'dividend'
      ? `This will remove ${application.cash_adjustment} of dividend cash/realized income for ${application.affected_quantity} shares.`
      : `This will remove the ${application.quantity_adjustment} share adjustment applied to ${application.affected_quantity} shares.`;
    const extra = application.action_type === 'split' || application.action_type === 'bonus'
      ? ' Reversal is rejected if later trades exist for this instrument.'
      : '';
    if (!window.confirm(`Reverse ${application.symbol} ${application.action_type} dated ${application.ex_date}?\n\n${changes}\nThe portfolio ledger will be recalculated from the original transactions.${extra}`)) return;
    setRowErrors((current) => ({ ...current, [application.id]: '' }));
    try {
      await apiRequest<Application>(`/corporate-actions/portfolios/${portfolioId}/applications/${application.id}/reverse`, { method: 'POST' });
      setNotice(`${application.symbol} corporate-action application reversed.`);
      await refresh();
    } catch (requestError) {
      setRowErrors((current) => ({ ...current, [application.id]: guidance(requestError, 'reverse') }));
    }
  }

  const selected = instrumentChoices.find((instrument) => instrument.id === instrumentId);
  const activeApplications = new Set(applications.filter((item) => item.active).map((item) => item.corporate_action_id));
  const selectedIsWatchlisted = Boolean(selected && watchlist.some((item) => item.instrument_id === selected.id));

  return (
    <div className="backfill-page">
      <p className="decision-disclaimer">Decision support, not investment advice. Corporate actions adjust recorded ledger history; they do not place trades.</p>
      {error && <p className="form-error" role="alert">{error}</p>}
      {notice && <p className="alerts-notice" role="status">{notice}</p>}
      <div className="content-grid">
        <section className="data-section">
          <div className="section-heading"><div><p className="eyebrow">USER-ENTERED EVENTS</p><h2>Corporate actions</h2></div></div>
          {actions.length === 0 ? <p className="table-empty">No corporate actions recorded.</p> : actions.map((action) => (
            <article className="backfill-card" key={action.id}>
              <header><strong>{action.symbol} · {action.action_type}</strong><span>{action.ex_date}</span></header>
              <p>{action.action_type === 'split' ? `Post-action share multiplier ${action.ratio} (2 means a 2-for-1 / 1:2 split).` : action.action_type === 'bonus' ? `${action.ratio} additional shares per existing share.` : `${action.amount} per share dividend.`}</p>
              {portfolioId && <button className="secondary-button" type="button" disabled={activeApplications.has(action.id)} onClick={() => void applyAction(action)}>{activeApplications.has(action.id) ? 'Applied to portfolio' : 'Apply to portfolio'}</button>}
              {rowErrors[action.id] && <p className="form-error" role="alert">{rowErrors[action.id]}</p>}
            </article>
          ))}
        </section>
        <section className="entry-section">
          <div className="section-heading"><div><p className="eyebrow">MANUAL ENTRY</p><h2>Create action</h2></div></div>
          <form className="form-stack" onSubmit={createAction}>
            <label>Find instrument<input value={instrumentQuery} onChange={(event) => setInstrumentQuery(event.target.value)} placeholder="Search symbol or name" /></label>
            <button className="secondary-button" type="button" onClick={() => void searchInstruments()} disabled={searchingInstruments || !instrumentQuery.trim()}>{searchingInstruments ? 'Searching…' : 'Search instruments'}</button>
            <label>Instrument<select value={instrumentId} onChange={(event) => setInstrumentId(event.target.value)} required><option value="">Choose a searched instrument</option>{instrumentChoices.map((item) => <option key={item.id} value={item.id}>{item.symbol} · {item.display_name} · {item.venue}</option>)}</select></label>
            {selected && <>
              <p className="valuation-footnote">Selected {selected.symbol} · {selected.currency}</p>
              <button className="secondary-button" type="button" onClick={() => void (selectedIsWatchlisted ? removeInstrumentFromWatchlist(selected.id) : addInstrumentToWatchlist(selected.id, selected.symbol))}>{selectedIsWatchlisted ? 'Remove from watchlist' : 'Add to watchlist'}</button>
            </>}
            <label>Action type<select value={actionType} onChange={(event) => setActionType(event.target.value as Action['action_type'])}><option value="split">Split</option><option value="bonus">Bonus</option><option value="dividend">Dividend</option></select></label>
            <label>Ex-date<input type="date" value={exDate} onChange={(event) => setExDate(event.target.value)} required /></label>
            {actionType === 'dividend'
              ? <label>Amount per share<input inputMode="decimal" value={amount} onChange={(event) => setAmount(event.target.value)} required /></label>
              : <label>{actionType === 'split' ? 'Post-action share multiplier' : 'Additional shares per existing share'}<input inputMode="decimal" value={ratio} onChange={(event) => setRatio(event.target.value)} required /><small>{actionType === 'split' ? 'Example: 2 for a 2-for-1 / 1:2 split.' : 'Example: 1 for a 1:1 bonus.'}</small></label>}
            <button className="primary-button" type="submit" disabled={busy || !instrumentId}>{busy ? 'Saving…' : 'Create action'}</button>
          </form>
        </section>
      </div>
      <section className="data-section backfill-list">
        <div className="section-heading"><div><p className="eyebrow">PORTFOLIO LEDGER</p><h2>Applications</h2></div></div>
        {!portfolioId ? <p>Select a portfolio to view applications.</p> : applications.length === 0 ? <p>No corporate actions have been applied.</p> : applications.map((application) => (
          <article className="backfill-card" key={application.id}>
            <header><strong>{application.symbol} · {application.action_type} · {application.ex_date}</strong><span>{application.active ? 'Active' : 'Reversed'}</span></header>
            <p>Affected quantity {application.affected_quantity} · share adjustment {application.quantity_adjustment} · cash adjustment {application.cash_adjustment}</p>
            {application.active && <button className="secondary-button" type="button" onClick={() => void reverseApplication(application)}>Reverse action</button>}
            {rowErrors[application.id] && <p className="form-error" role="alert">{rowErrors[application.id]}</p>}
          </article>
        ))}
      </section>
    </div>
  );
}
