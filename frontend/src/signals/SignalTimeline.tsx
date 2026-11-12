import { useCallback, useEffect, useState } from 'react';
import { lab, messageOf } from './lab';
import { LifecycleChip } from './LifecycleChip';
import { fmtIst, isTerminal, reasonLabel, withTimeout } from './lifecycle';
import { ConfirmDialog, Empty, Notice } from './shared';
import type { SignalEvent, SignalRow } from './types';

/** Append-only history from /signals/{id}/events. "Recorded" = when the app wrote the event; "Market data as-of" = the candle data it was based on. */
export function SignalTimeline({ signal, onUpdated }: { signal: SignalRow; onUpdated: (row: SignalRow) => void }) {
  const [events, setEvents] = useState<SignalEvent[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [cancelError, setCancelError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try { const got = await withTimeout(lab<SignalEvent[]>(`/signals/${signal.id}/events`));
      setEvents(Array.isArray(got) ? got.filter((e) => typeof e?.to_state === 'string') : []); } catch (e) { setEvents(null); setError(messageOf(e)); }
  }, [signal.id]);
  useEffect(() => { setEvents(null); void load(); }, [load]);

  const cancel = async () => {
    setBusy(true); setCancelError(null);
    try {
      const updated = await withTimeout(lab<SignalRow>(`/signals/${signal.id}/cancel`, { method: 'POST', body: {} }));
      setConfirming(false);
      onUpdated(updated);
      await load();
    } catch (e) { setCancelError(messageOf(e)); setConfirming(false); } finally { setBusy(false); }
  };

  return (
    <section aria-label="Signal timeline">
      <h4>Lifecycle timeline</h4>
      <p className="sl-muted">
        <strong>Recorded</strong> is when this application wrote the event. <strong>Market data as-of</strong> is the candle time the event was based on. They are different things.
      </p>
      {error && <Notice tone="bad">Could not load the timeline: {error} <button type="button" className="sl-link" onClick={() => void load()}>Retry</button></Notice>}
      {!error && events === null && <p className="sl-muted" role="status">Loading timeline… (gives up after 15 s)</p>}
      {events !== null && events.length === 0 && <Empty title="No events recorded">This signal has no lifecycle events.</Empty>}
      {events !== null && events.length > 0 && (
        <ol className="sl-timeline">
          {events.map((e) => (
            <li key={e.event_id}>
              <div>
                {e.from_state ? <LifecycleChip status={e.from_state} /> : <span className="sl-muted">created</span>} → <LifecycleChip status={e.to_state} />
                {' '}<span title={e.reason_code}>{reasonLabel(e.reason_code)}</span>{e.backfilled && <span className="sl-backfilled" title="Written by the migration; the original market-data time was never known">backfilled</span>}
              </div>
              <dl>
                <dt>Recorded</dt><dd>{fmtIst(e.event_time)}</dd>
                <dt>Market data as-of</dt><dd>{e.observed_data_time ? fmtIst(e.observed_data_time) : <span title="No market-data time was recorded for this event">—</span>}</dd>
              </dl>
              {e.reason_detail && <p className="sl-muted">{e.reason_detail}</p>}
            </li>
          ))}
        </ol>
      )}
      {!isTerminal(signal.status) ? (
        <p><button type="button" className="sl-danger-text" disabled={busy} onClick={() => setConfirming(true)}>Cancel signal</button></p>
      ) : (
        <p className="sl-muted">This signal is in a final state; it can no longer be cancelled.</p>
      )}
      {cancelError && <Notice tone="bad">Cancel failed: {cancelError}</Notice>}
      {confirming && (
        <ConfirmDialog title={`Cancel ${signal.symbol ?? 'this'} signal?`} confirmLabel={busy ? 'Cancelling…' : 'Cancel signal'} danger onCancel={() => setConfirming(false)} onConfirm={() => void cancel()}
          body={<p>This records a CANCELLED transition on the signal's history. It does not place or cancel any order, and it cannot be undone.</p>} />
      )}
    </section>
  );
}
