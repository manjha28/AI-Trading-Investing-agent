import { useState } from 'react';
import { apiRequest } from '../api';
import { refreshExecutionState } from '../hooks/useExecutionState';
import { explainState, SHORT, venueName, type VenueState } from '../lib/executionState';
import { ArmConfirm } from './ArmConfirm';
import { Chip } from './ui/kit';

const PATHS = { dhan: '/dhan-live', delta: '/crypto-delta/live' } as const;

/** The venue's effective state as a button: hover explains it; a click arms (after the warning dialog) or disarms at once. */
export function ExecutionStateChip({ venue, onChanged }: { venue: VenueState | null | undefined; onChanged?: () => void }) {
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  if (!venue) return <Chip tone="neutral" title="The server has not reported the execution state yet">Execution state unknown</Chip>;
  const s = venue.effective.state;
  const name = venueName(venue);
  const run = async (action: 'arm' | 'disarm') => {
    setBusy(true); setError(null);
    try {
      await apiRequest(`${PATHS[venue.venue]}/${action}`, { method: 'POST', body: JSON.stringify(action === 'arm' ? { acknowledged: true } : {}) });
      await refreshExecutionState(); onChanged?.();
    } catch (e) { setError(e instanceof Error ? e.message : 'Could not change the live switch.'); } finally { setBusy(false); setConfirming(false); }
  };
  return (
    <span className="live-switch">
      <button type="button" disabled={busy} title={explainState(venue)} style={{ background: 'none', border: 0, padding: 0, cursor: 'pointer', font: 'inherit' }}
        onClick={() => (venue.armed ? void run('disarm') : setConfirming(true))}>
        <Chip tone={s === 'LIVE' ? 'negative' : s === 'ARMED_BUT_BLOCKED' ? 'caution' : 'neutral'}>{name}: {SHORT[s]}</Chip>
      </button>
      {error && <span role="alert" className="live-switch-error"> {error}</span>}
      {confirming && <ArmConfirm open venue={name} strategies={venue.strategies.map((x) => x.key)} quantityCap={venue.venue === 'dhan' ? '1 share per order' : '1 contract per order'}
        leverage={venue.venue === 'delta' ? 'the lower of 25× and the exchange maximum' : undefined} tradingWindow={venue.window.text} busy={busy} onConfirm={() => void run('arm')} onCancel={() => setConfirming(false)} />}
    </span>
  );
}
