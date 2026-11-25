import { useEffect, useRef, useState } from 'react';
import { SHORT, venueName, type ExecutionStates } from '../lib/executionState';

type Props = {
  states: ExecutionStates | null;
  marketLabel: string;
  marketStatus: string;
  canSync: boolean;
  syncing: boolean;
  onSync: () => void;
  onOpenBrokers: () => void;
};

/** One top-bar pill for the live state: "Live: off" (grey) or "Live: Delta on" (red). The popover holds the per-venue state, market-data state, Dhan sync and connection. */
export function StatusPill({ states, marketLabel, marketStatus, canSync, syncing, onSync, onOpenBrokers }: Props) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return undefined;
    const close = (e: MouseEvent) => { if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false); };
    document.addEventListener('mousedown', close);
    return () => document.removeEventListener('mousedown', close);
  }, [open]);
  const venues = states ? [states.dhan, states.delta] : [];
  const live = venues.filter((v) => v.effective.state === 'LIVE');
  const blocked = venues.filter((v) => v.effective.state === 'ARMED_BUT_BLOCKED');
  const label = !states ? 'Live: checking…' : live.length ? `Live: ${live.map(venueName).join(' + ')} on` : blocked.length ? `Live: ${blocked.map(venueName).join(' + ')} armed, blocked` : 'Live: off';
  const tone = live.length ? 'live' : blocked.length ? 'blocked' : 'off';
  return (
    <div className="status-pill-wrap" ref={ref}>
      <button type="button" className={`status-pill status-pill-${tone}`} aria-expanded={open} aria-haspopup="dialog" onClick={() => setOpen((o) => !o)}>
        <span className={`status-dot ${marketStatus}`} aria-hidden="true" />{label}
      </button>
      {open && (
        <div className="status-popover" role="dialog" aria-label="Live trading and data status">
          {venues.map((v) => (
            <div key={v.venue} className="status-popover-row">
              <b>{venueName(v)}: {SHORT[v.effective.state]}</b>
              <small>{v.effective.reasons.join('; ') || 'Orders can be sent now'}</small>
              <small>{Object.entries(v.limits).map(([k, val]) => `${k.replace(/_/g, ' ')} ${val}`).join(' · ')}</small>
            </div>
          ))}
          <div className="status-popover-row"><b>Market data</b><small>{marketLabel}</small></div>
          <div className="status-popover-actions">
            <button className="secondary-button" type="button" disabled={!canSync || syncing} onClick={onSync}>{syncing ? 'Syncing Dhan…' : 'Sync Dhan account'}</button>
            <button className="text-button" type="button" onClick={() => { setOpen(false); onOpenBrokers(); }}>Dhan connection</button>
          </div>
        </div>
      )}
    </div>
  );
}
