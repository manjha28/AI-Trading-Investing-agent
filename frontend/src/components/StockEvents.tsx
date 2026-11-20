import { useEffect, useState } from 'react';
import { apiRequest, ApiRequestError } from '../api';

type Action = { id: string; action_type: string; ex_date: string; announcement_date: string | null; record_date: string | null; ratio: string | null; amount: string | null; source: string; verified_at: string | null; is_discrepant: boolean; discrepancy_note: string | null };
type NewsItem = { external_id: string; source: string; source_url: string | null; instrument_id: string | null; published_at: string; publisher: string | null; category: string | null; headline: string };

const KIND_LABEL: Record<string, string> = { split: 'Stock split', bonus: 'Bonus issue', dividend: 'Dividend', rights: 'Rights issue', merger: 'Merger', demerger: 'Demerger', buyback: 'Buyback' };
const messageOf = (e: unknown) => (e instanceof ApiRequestError || e instanceof Error ? e.message : 'Request failed.');
const day = (iso: string) => new Date(`${iso}T00:00:00`).toLocaleDateString('en-IN', { dateStyle: 'medium' });
const detail = (a: Action) => (a.ratio ? `ratio ${a.ratio}` : a.amount ? `₹${a.amount} per share` : '');

/** Corporate actions (splits, bonuses, dividends) and the latest announcements/news for ONE stock. */
export function StockEvents({ instrumentId, symbol }: { instrumentId: string; symbol: string }) {
  const [actions, setActions] = useState<Action[] | null>(null);
  const [news, setNews] = useState<NewsItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setActions(null); setNews(null); setError(null);
    apiRequest<Action[]>(`/corporate-actions?instrument_id=${instrumentId}`).then((r) => { if (!cancelled) setActions(r); }).catch((e) => { if (!cancelled) { setActions([]); setError(messageOf(e)); } });
    apiRequest<NewsItem[]>(`/news?instrument_id=${instrumentId}&limit=30`).then((r) => { if (!cancelled) setNews(r.filter((n) => n.instrument_id === instrumentId).slice(0, 12)); }).catch(() => { if (!cancelled) setNews([]); });
    return () => { cancelled = true; };
  }, [instrumentId]);

  const today = new Date().toISOString().slice(0, 10);
  const upcoming = (actions ?? []).filter((a) => a.ex_date >= today);
  const past = (actions ?? []).filter((a) => a.ex_date < today).slice(0, 8);

  return (
    <div className="stock-panel" aria-label={`Corporate actions and news for ${symbol}`}>
      {error && <p className="alerts-notice" role="alert">{error}</p>}
      <h4 className="stock-h">Corporate actions</h4>
      {actions === null ? <p className="crypto-sub">Loading…</p> : actions.length === 0 ? <p className="crypto-sub">No split, bonus or dividend is recorded for {symbol}.</p> : (
        <ul className="stock-list">
          {[...upcoming, ...past].map((a) => (
            <li key={a.id}>
              <span><strong>{KIND_LABEL[a.action_type] ?? a.action_type}</strong> <small>ex-date {day(a.ex_date)}{detail(a) ? ` · ${detail(a)}` : ''}</small>
                {a.ex_date >= today && <em className="stock-upcoming"> · upcoming</em>}{a.is_discrepant && <em className="negative" title={a.discrepancy_note ?? undefined}> · sources disagree</em>}</span>
              <small>{a.verified_at ? 'verified' : a.source}</small>
            </li>))}
        </ul>
      )}
      <h4 className="stock-h">Latest announcements and news</h4>
      {news === null ? <p className="crypto-sub">Loading…</p> : news.length === 0 ? <p className="crypto-sub">No announcement or news is stored for {symbol} yet.</p> : (
        <ul className="stock-list">
          {news.map((n) => (
            <li key={`${n.source}:${n.external_id}`}>
              <span>{n.source_url ? <a href={n.source_url} target="_blank" rel="noreferrer noopener">{n.headline}</a> : n.headline}<br /><small>{[n.publisher ?? n.source, n.category, new Date(n.published_at).toLocaleDateString('en-IN', { dateStyle: 'medium' })].filter(Boolean).join(' · ')}</small></span>
            </li>))}
        </ul>
      )}
      <p className="crypto-sub">Public filings and news as ingested; decision support, not investment advice.</p>
    </div>
  );
}
