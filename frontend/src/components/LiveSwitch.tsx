import { useState } from 'react';
import { apiRequest } from '../api';
import { refreshExecutionState } from '../hooks/useExecutionState';
import { explainState, venueName, type VenueState } from '../lib/executionState';
import { ArmConfirm } from './ArmConfirm';

const PATHS = { dhan: '/dhan-live', delta: '/crypto-delta/live' } as const;

/** One button per venue: "Dhan not live" turns live orders on after a warning (nothing to type); "Dhan LIVE" turns them off at once. */
export function LiveSwitch({ venue }: { venue: VenueState | null | undefined }) {
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  if (!venue) return null;
  const on = venue.armed;
  const name = venueName(venue);
  const run = async (action: 'arm' | 'disarm') => {
    setBusy(true); setError(null);
    try {
      await apiRequest(`${PATHS[venue.venue]}/${action}`, { method: 'POST', body: JSON.stringify(action === 'arm' ? { acknowledged: true } : {}) });
      await refreshExecutionState(); setConfirming(false);
    } catch (e) { setError(e instanceof Error ? e.message : 'Could not change the live switch.'); setConfirming(false); } finally { setBusy(false); }
  };
  const live = venue.effective.state === 'LIVE';
  return (
    <span className="live-switch">
      <button type="button" className={`live-switch-btn ${on ? 'is-on' : ''}`} disabled={busy} aria-pressed={on}
        title={explainState(venue)}
        onClick={() => (on ? void run('disarm') : setConfirming(true))}>
        {name} {live ? 'LIVE' : on ? 'on · waiting' : 'not live'}
      </button>
      {on && <button type="button" className="live-switch-unarm" disabled={busy} onClick={() => void run('disarm')} aria-label={`Unarm ${name} live orders`}>Unarm</button>}
      {error && <span role="alert" className="live-switch-error"> {error}</span>}
      <ArmConfirm open={confirming} venue={name} strategies={venue.strategies.map((s) => s.key)} quantityCap={venue.venue === 'dhan' ? '1 share per order' : '1 contract per order'}
        leverage={venue.venue === 'delta' ? 'the lower of 25× and the exchange maximum' : undefined} tradingWindow={venue.window.text} busy={busy} onConfirm={() => void run('arm')} onCancel={() => setConfirming(false)} />
    </span>
  );
}
