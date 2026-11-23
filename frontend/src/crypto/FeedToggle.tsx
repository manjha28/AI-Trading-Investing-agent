import { useCallback, useEffect, useState } from 'react';
import { apiRequest } from '../api';
import { messageOf } from './cryptoApi';

type Feed = { available: boolean; wanted: boolean; state: string | null; connected: boolean | null; last_event_at: string | null };

/** Pause or resume the public Delta feed to save hosting cost. The choice is stored on the server and survives restarts; stored prices stay readable. */
export function FeedToggle({ onChange }: { onChange?: (wanted: boolean) => void }) {
  const [feed, setFeed] = useState<Feed | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const load = useCallback(async () => {
    try { const f = await apiRequest<Feed>('/crypto-delta/feed'); setFeed(f); onChange?.(f.wanted); } catch (e) { setError(messageOf(e)); }
  }, [onChange]);
  useEffect(() => { void load(); const t = window.setInterval(() => void load(), 15_000); return () => window.clearInterval(t); }, [load]);

  async function flip() {
    if (!feed) return;
    setBusy(true); setError('');
    try { const f = await apiRequest<Feed>('/crypto-delta/feed', { method: 'PUT', body: JSON.stringify({ enabled: !feed.wanted }) }); setFeed(f); onChange?.(f.wanted); } catch (e) { setError(messageOf(e)); } finally { setBusy(false); }
  }

  if (!feed) return error ? <p role="alert" className="crypto-error">{error}</p> : null;
  if (!feed.available) return <span className="crypto-sub">Feed not configured on this server.</span>;
  const live = feed.wanted && feed.connected;
  return (
    <span className="feed-toggle">
      <span className={`feed-dot ${live ? 'on' : feed.wanted ? 'wait' : 'off'}`} aria-hidden="true" />
      <span role="status">{!feed.wanted ? 'Feed paused' : live ? 'Feed live' : 'Feed starting…'}</span>
      <button type="button" className="secondary-button" onClick={() => void flip()} disabled={busy}>{feed.wanted ? 'Pause feed (save cost)' : 'Resume feed'}</button>
      {error && <span role="alert" className="crypto-error">{error}</span>}
    </span>
  );
}
