import { fmtTime } from './lab';

/**
 * Lifecycle state → presentation. Colour encodes WHERE THE SIGNAL IS IN ITS LIFE, never how it turned out (blueprint §5.3):
 * TARGET and STOPPED share one style because both are "finished by its own levels"; neither is green or red.
 *   in progress ........ accent (blue) family; FORMING is hollow/dashed, READY outlined, ACTIVE solid, PARTIALLY_REALISED solid with a split marker
 *   finished (resolved)  neutral slate, solid outline: TARGET, STOPPED
 *   ended unresolved .... muted, dashed outline: INVALIDATED, EXPIRED, CANCELLED
 * Every chip also carries its text label and a glyph, so state is never conveyed by colour alone.
 */
export type LifecycleState = 'forming' | 'ready' | 'active' | 'partially_realised' | 'target' | 'stopped' | 'invalidated' | 'expired' | 'cancelled';
export type StateGroup = 'in_progress' | 'resolved' | 'ended';

export const STATE_META: Record<LifecycleState, { label: string; group: StateGroup; glyph: string }> = {
  forming: { label: 'FORMING', group: 'in_progress', glyph: '◌' },
  ready: { label: 'READY', group: 'in_progress', glyph: '○' },
  active: { label: 'ACTIVE', group: 'in_progress', glyph: '●' },
  partially_realised: { label: 'PARTIALLY REALISED', group: 'in_progress', glyph: '◐' },
  target: { label: 'TARGET', group: 'resolved', glyph: '■' },
  stopped: { label: 'STOPPED', group: 'resolved', glyph: '■' },
  invalidated: { label: 'INVALIDATED', group: 'ended', glyph: '╳' },
  expired: { label: 'EXPIRED', group: 'ended', glyph: '⌛' },
  cancelled: { label: 'CANCELLED', group: 'ended', glyph: '∅' },
};
export const GROUP_LABEL: Record<StateGroup, string> = { in_progress: 'In progress', resolved: 'Finished (resolved)', ended: 'Ended without resolution' };
export const STATE_ORDER = Object.keys(STATE_META) as LifecycleState[];
const TERMINAL: ReadonlySet<string> = new Set(['target', 'stopped', 'invalidated', 'expired', 'cancelled']);

export const isLifecycleState = (value: string): value is LifecycleState => value in STATE_META;
/** Unknown or legacy values are NOT treated as terminal: the server decides, and rejects a cancel it does not allow. */
export const isTerminal = (status: string): boolean => TERMINAL.has(status);

export const REASON_LABELS: Record<string, string> = {
  // event reasons
  signal_created: 'Signal created', confirmed_next_scan: 'Confirmed on the next scan', exit_condition: 'Exit condition met', opposite_signal: 'Opposite signal appeared',
  time_expired: 'Time window expired', owner_cancel: 'Cancelled by owner', target_reached: 'Target reached', stop_hit: 'Stop hit',
  partial_realisation: 'Partially realised', conditions_complete: 'All conditions complete', backfill_genesis: 'Recorded by migration (original data time unknown)',
  // scan rejection reasons
  insufficient_bars: 'Not enough bars', stale_data: 'Stale data', synthetic_data: 'Synthetic data', missing_data_limit: 'Too many missing candles', stale_spot: 'Stale underlying spot',
  no_history: 'No stored history', other: 'Other',
};
export const reasonLabel = (code: string | null | undefined): string => (code ? REASON_LABELS[code] ?? code : '—');

/** Every timestamp shown to the user states its zone. */
export const fmtIst = (iso: string | null | undefined): string => (iso ? `${fmtTime(iso)} IST` : '—');

export function withTimeout<T>(promise: Promise<T>, ms = 15_000): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`No response after ${Math.round(ms / 1000)} s`)), ms);
    promise.then((v) => { clearTimeout(timer); resolve(v); }, (e: unknown) => { clearTimeout(timer); reject(e instanceof Error ? e : new Error(String(e))); });
  });
}

export type RejectionSummary = { scan_run_id: string | null; total: number; by_reason: Record<string, number> };
export type ScanResult = {
  scan_run_id: string; status?: string; trigger?: string | null; started_at?: string | null; strategies?: number; pairs_scanned: number; instruments_scanned: number; new_signals: number;
  errors: { strategy?: string; instrument?: string; error: string }[]; rejections: RejectionSummary;
};

/** "2 new signals · 7 suppressed (Not enough bars 6, Stale underlying spot 1)". Counts come from the scan response only. */
export function suppressionText(r: RejectionSummary): string {
  const parts = Object.entries(r.by_reason).map(([code, n]) => `${reasonLabel(code)} ${n}`);
  return `${r.total} suppressed${parts.length ? ` (${parts.join(', ')})` : ''}`;
}
