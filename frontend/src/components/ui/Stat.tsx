import type { ReactNode } from 'react';
import { formatIST, formatPct } from '../../lib/format';

/** A headline number: label, value, optional delta with an arrow, and one footer line with the as-of time and source. */
export function Stat({ label, value, delta, asOf, source }: { label: string; value: ReactNode; delta?: number | null; asOf?: string | null; source?: string }) {
  const tone = delta === null || delta === undefined || delta === 0 ? '' : delta > 0 ? 'ui-tone-positive' : 'ui-tone-negative';
  return (
    <div className="ui-stat">
      <span className="ui-stat-label">{label}</span>
      <strong className="ui-stat-value">{value}</strong>
      {delta !== undefined && <span className={`ui-stat-delta ${tone}`}>{formatPct(delta, { arrow: true })}</span>}
      {(asOf || source) && <span className="ui-stat-foot">{[asOf ? `as of ${formatIST(asOf)}` : null, source].filter(Boolean).join(' · ')}</span>}
    </div>
  );
}
