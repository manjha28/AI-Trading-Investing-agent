import { useEffect, useState, type FormEvent } from 'react';
import { ApiRequestError, apiRequest } from '../api';

type TargetLevel = { price: string; trim_pct: string; note: string | null; distance_pct: string | null };
type Condition = { type: 'price_below' | 'price_above' | 'loss_from_cost_pct'; value: string; note?: string | null };
type Market = {
  last_price: string | null;
  freshness: 'fresh' | 'stale' | 'unavailable';
  is_synthetic: boolean;
  source: string | null;
  actionable: boolean;
  note: string;
};
export type Thesis = {
  id: string;
  symbol: string;
  status: 'active' | 'invalidated' | 'closed';
  entry_reason: string;
  horizon_days: number;
  conviction: number;
  currency: string;
  target_levels: TargetLevel[];
  stop_level: string | null;
  stop_distance_pct: string | null;
  invalidation_conditions: Condition[];
  invalidation_notes: string | null;
  max_loss_pct: string | null;
  review_at: string | null;
  review_overdue: boolean;
  market: Market;
};
type Revision = { revision_number: number; changed_fields: string[]; snapshot: { conviction: number; status: string }; created_at: string };
type LevelDraft = { price: string; trim_pct: string; note: string };
type ConditionDraft = { type: Condition['type']; value: string };

type Props = {
  portfolioId: string;
  currency: string;
  symbol: string;
  instrumentId: string | null;
  onClose: () => void;
  onChanged: () => void;
};

const conditionLabels: Record<Condition['type'], string> = {
  price_below: 'Price falls below',
  price_above: 'Price rises above',
  loss_from_cost_pct: 'Loss from cost exceeds (%)',
};

function formatDistance(value: string | null): string {
  if (value === null) return '—';
  const number = Number(value);
  return `${number > 0 ? '+' : ''}${number.toFixed(2)}%`;
}

export function ThesisEditor({ portfolioId, currency, symbol, instrumentId, onClose, onChanged }: Props) {
  const [thesis, setThesis] = useState<Thesis | null>(null);
  const [revisions, setRevisions] = useState<Revision[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [entryReason, setEntryReason] = useState('');
  const [horizonDays, setHorizonDays] = useState('365');
  const [conviction, setConviction] = useState('3');
  const [status, setStatus] = useState<Thesis['status']>('active');
  const [levels, setLevels] = useState<LevelDraft[]>([]);
  const [stopLevel, setStopLevel] = useState('');
  const [conditions, setConditions] = useState<ConditionDraft[]>([]);
  const [invalidationNotes, setInvalidationNotes] = useState('');
  const [maxLoss, setMaxLoss] = useState('');
  const [reviewAt, setReviewAt] = useState('');

  const base = `/portfolios/${portfolioId}/theses`;

  function load(next: Thesis | null) {
    setThesis(next);
    setEntryReason(next?.entry_reason ?? '');
    setHorizonDays(String(next?.horizon_days ?? 365));
    setConviction(String(next?.conviction ?? 3));
    setStatus(next?.status ?? 'active');
    setLevels((next?.target_levels ?? []).map((level) => ({ price: level.price, trim_pct: level.trim_pct, note: level.note ?? '' })));
    setStopLevel(next?.stop_level ?? '');
    setConditions((next?.invalidation_conditions ?? []).map((item) => ({ type: item.type, value: item.value })));
    setInvalidationNotes(next?.invalidation_notes ?? '');
    setMaxLoss(next?.max_loss_pct ?? '');
    setReviewAt(next?.review_at ? next.review_at.slice(0, 10) : '');
  }

  useEffect(() => {
    let active = true;
    setLoading(true);
    setError('');
    apiRequest<Thesis[]>(base)
      .then(async (all) => {
        const match = instrumentId ? all.filter((item) => item.symbol === symbol) : [];
        const current = match.find((item) => item.status === 'active') ?? match[0] ?? null;
        const history = current ? await apiRequest<Revision[]>(`${base}/${current.id}/revisions`) : [];
        if (!active) return;
        load(current);
        setRevisions(history);
      })
      .catch((requestError: Error) => {
        if (active) setError(requestError.message);
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [base, symbol, instrumentId]);

  async function save(event: FormEvent) {
    event.preventDefault();
    if (!instrumentId) return;
    setSaving(true);
    setError('');
    const body = {
      currency,
      status,
      entry_reason: entryReason,
      horizon_days: Number(horizonDays),
      conviction: Number(conviction),
      target_levels: levels.filter((level) => level.price).map((level) => ({
        price: level.price,
        trim_pct: level.trim_pct,
        note: level.note || null,
      })),
      stop_level: stopLevel || null,
      invalidation_conditions: conditions.filter((item) => item.value).map((item) => ({ type: item.type, value: item.value })),
      invalidation_notes: invalidationNotes || null,
      max_loss_pct: maxLoss || null,
      review_at: reviewAt ? `${reviewAt}T00:00:00Z` : null,
    };
    try {
      const saved = thesis
        ? await apiRequest<Thesis>(`${base}/${thesis.id}`, { method: 'PATCH', body: JSON.stringify(body) })
        : await apiRequest<Thesis>(base, { method: 'POST', body: JSON.stringify({ ...body, symbol, instrument_id: instrumentId }) });
      load(saved);
      setRevisions(await apiRequest<Revision[]>(`${base}/${saved.id}/revisions`));
      onChanged();
    } catch (requestError) {
      setError(requestError instanceof ApiRequestError ? requestError.message : 'The thesis could not be saved.');
    } finally {
      setSaving(false);
    }
  }

  async function remove() {
    if (!thesis || !window.confirm('Delete this thesis and its revision history?')) return;
    try {
      await apiRequest<void>(`${base}/${thesis.id}`, { method: 'DELETE' });
      load(null);
      setRevisions([]);
      onChanged();
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : 'The thesis could not be deleted.');
    }
  }

  const market = thesis?.market;
  const flag = market ? (market.is_synthetic ? 'synthetic' : market.freshness) : null;

  return (
    <div className="thesis-overlay" role="dialog" aria-modal="true" aria-label={`${symbol} thesis`}>
      <div className="thesis-sheet">
        <header className="thesis-header">
          <div><p className="eyebrow">POSITION THESIS</p><h2>{symbol}</h2></div>
          <button className="secondary-button" type="button" onClick={onClose}>Close</button>
        </header>
        <p className="thesis-disclaimer">Decision support, not investment advice. Levels are your own plan; nothing here places an order.</p>
        {loading ? <p>Loading…</p> : !instrumentId ? (
          <div className="data-note"><span>Link this holding to a market instrument before writing a thesis, so levels can be checked against a price in {currency}.</span></div>
        ) : (
          <form className="thesis-form" onSubmit={save}>
            {error && <div className="form-error" role="alert">{error}</div>}
            {thesis && market && (
              <section className="thesis-market">
                <div>
                  <span>Last price</span>
                  <strong>{market.last_price ? `${market.last_price} ${thesis.currency}` : 'Unavailable'}</strong>
                  {flag && <span className={`freshness-badge ${market.is_synthetic ? 'freshness-synthetic' : `freshness-${market.freshness}`}`}>{flag}</span>}
                </div>
                <small>{market.note}</small>
                <div className="thesis-distances">
                  {thesis.target_levels.map((level, index) => (
                    <div key={`${level.price}-${index}`}><span>Target {index + 1} · {level.price}</span><strong>{formatDistance(level.distance_pct)}</strong></div>
                  ))}
                  {thesis.stop_level && <div><span>Stop · {thesis.stop_level}</span><strong>{formatDistance(thesis.stop_distance_pct)}</strong></div>}
                </div>
                {thesis.review_overdue && <small className="thesis-warning">Review is overdue.</small>}
              </section>
            )}
            <label>Why I own it<textarea value={entryReason} onChange={(e) => setEntryReason(e.target.value)} required maxLength={4000} rows={3} /></label>
            <div className="thesis-row">
              <label>Horizon (days)<input type="number" min={1} max={3650} value={horizonDays} onChange={(e) => setHorizonDays(e.target.value)} required /></label>
              <label>Conviction<select value={conviction} onChange={(e) => setConviction(e.target.value)}>{[1, 2, 3, 4, 5].map((n) => <option key={n} value={n}>{n}</option>)}</select></label>
              {thesis && <label>Status<select value={status} onChange={(e) => setStatus(e.target.value as Thesis['status'])}><option value="active">Active</option><option value="invalidated">Invalidated</option><option value="closed">Closed</option></select></label>}
            </div>
            <fieldset>
              <legend>Target ladder ({currency}) · total trim ≤ 100%</legend>
              {levels.map((level, index) => (
                <div className="thesis-row" key={index}>
                  <label>Price<input inputMode="decimal" value={level.price} onChange={(e) => setLevels(levels.map((item, i) => i === index ? { ...item, price: e.target.value } : item))} /></label>
                  <label>Trim %<input inputMode="decimal" value={level.trim_pct} onChange={(e) => setLevels(levels.map((item, i) => i === index ? { ...item, trim_pct: e.target.value } : item))} /></label>
                  <label>Note<input value={level.note} maxLength={500} onChange={(e) => setLevels(levels.map((item, i) => i === index ? { ...item, note: e.target.value } : item))} /></label>
                  <button className="secondary-button" type="button" onClick={() => setLevels(levels.filter((_, i) => i !== index))}>Remove</button>
                </div>
              ))}
              {levels.length < 10 && <button className="secondary-button" type="button" onClick={() => setLevels([...levels, { price: '', trim_pct: '', note: '' }])}>Add target</button>}
            </fieldset>
            <div className="thesis-row">
              <label>Stop level ({currency})<input inputMode="decimal" value={stopLevel} onChange={(e) => setStopLevel(e.target.value)} /></label>
              <label>Max loss %<input inputMode="decimal" value={maxLoss} onChange={(e) => setMaxLoss(e.target.value)} /></label>
              <label>Review on<input type="date" value={reviewAt} onChange={(e) => setReviewAt(e.target.value)} /></label>
            </div>
            <fieldset>
              <legend>Invalidation conditions</legend>
              {conditions.map((item, index) => (
                <div className="thesis-row" key={index}>
                  <label>Condition<select value={item.type} onChange={(e) => setConditions(conditions.map((c, i) => i === index ? { ...c, type: e.target.value as Condition['type'] } : c))}>{Object.entries(conditionLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
                  <label>Value<input inputMode="decimal" value={item.value} onChange={(e) => setConditions(conditions.map((c, i) => i === index ? { ...c, value: e.target.value } : c))} /></label>
                  <button className="secondary-button" type="button" onClick={() => setConditions(conditions.filter((_, i) => i !== index))}>Remove</button>
                </div>
              ))}
              {conditions.length < 10 && <button className="secondary-button" type="button" onClick={() => setConditions([...conditions, { type: 'price_below', value: '' }])}>Add condition</button>}
              <label>Notes<textarea value={invalidationNotes} onChange={(e) => setInvalidationNotes(e.target.value)} maxLength={4000} rows={2} /></label>
            </fieldset>
            <div className="thesis-actions">
              <button className="primary-button" type="submit" disabled={saving}>{saving ? 'Saving…' : thesis ? 'Save changes' : 'Create thesis'}</button>
              {thesis && <button className="secondary-button" type="button" onClick={() => void remove()}>Delete</button>}
            </div>
            {revisions.length > 0 && <details className="thesis-history">
              <summary>View revision history ({revisions.length})</summary>
              <ol>{revisions.map((revision) => <li key={revision.revision_number}>#{revision.revision_number} · {new Date(revision.created_at).toLocaleString()} · conviction {revision.snapshot.conviction} · {revision.snapshot.status} · changed {revision.changed_fields.join(', ')}</li>)}</ol>
            </details>}
          </form>
        )}
      </div>
    </div>
  );
}
