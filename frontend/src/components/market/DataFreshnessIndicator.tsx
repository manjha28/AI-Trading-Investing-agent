export type DataProvenance = 'live' | 'cached' | 'delayed' | 'unavailable' | 'connecting';

const LABELS: Record<DataProvenance, string> = {
  live: 'Live',
  cached: 'Cached snapshot',
  delayed: 'Delayed · persisted',
  unavailable: 'Unavailable',
  connecting: 'Connecting',
};

export function formatObservedAge(observedAt: string | null | undefined, now = Date.now()): string {
  if (!observedAt) return 'no timestamp';
  const observed = Date.parse(observedAt);
  if (Number.isNaN(observed)) return 'no timestamp';
  const seconds = Math.max(0, Math.floor((now - observed) / 1000));
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  return `${Math.floor(seconds / 3600)}h ago`;
}

/**
 * Shows where a displayed value came from, how old it is, and when it was
 * observed. Used anywhere live and fallback data can appear together.
 */
export function DataFreshnessIndicator({
  provenance,
  observedAt,
  provider,
  detail,
}: {
  provenance: DataProvenance;
  observedAt?: string | null;
  provider?: string | null;
  detail?: string;
}) {
  const showAge = provenance !== 'unavailable' && provenance !== 'connecting' && observedAt !== undefined;
  return (
    <span className={`data-freshness data-freshness-${provenance}`} role="status">
      <span className="data-freshness-dot" aria-hidden="true" />
      <strong>{LABELS[provenance]}</strong>
      {provider && <span>· {provider}</span>}
      {showAge && <span>· {formatObservedAge(observedAt)}</span>}
      {detail && <span className="data-freshness-detail">· {detail}</span>}
    </span>
  );
}
