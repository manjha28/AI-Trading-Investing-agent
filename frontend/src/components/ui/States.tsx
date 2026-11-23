import { useState, type ReactNode } from 'react';
import { Button } from './Button';
import { formatIST } from '../../lib/format';

/** Loading placeholder shaped like what is coming: table rows, a card, or a stat. */
export function Skeleton({ variant = 'table', rows = 6 }: { variant?: 'table' | 'card' | 'stat'; rows?: number }) {
  const n = variant === 'stat' ? 1 : variant === 'card' ? 3 : rows;
  return <div className={`ui-skeleton ui-skeleton-${variant}`} role="status" aria-busy="true" aria-label="Loading">{Array.from({ length: n }, (_, i) => <span key={i} className="ui-skeleton-line" />)}</div>;
}

/** Nothing to show: one sentence, and the next action if there is one. */
export function EmptyState({ children, action }: { children: ReactNode; action?: ReactNode }) {
  return <div className="ui-empty" role="status"><p>{children}</p>{action}</div>;
}

/** A failed load: the real error text, Retry, and copyable details. Never a bare "No data". */
export function ErrorState({ error, onRetry, context }: { error: unknown; onRetry?: () => void; context?: string }) {
  const [copied, setCopied] = useState(false);
  const text = error instanceof Error ? error.message : String(error ?? 'Request failed');
  const details = `${context ? `${context}\n` : ''}${text}\n${new Date().toISOString()}`;
  return (
    <div className="ui-error" role="alert">
      <p><strong>Couldn&apos;t load this.</strong> {text}</p>
      {context && <p className="ui-muted">{context}</p>}
      <div className="ui-row">
        {onRetry && <Button variant="secondary" size="sm" onClick={onRetry}>Retry</Button>}
        <Button variant="ghost" size="sm" onClick={() => { void navigator.clipboard?.writeText(details).then(() => setCopied(true)).catch(() => undefined); }}>{copied ? 'Copied' : 'Copy details'}</Button>
      </div>
    </div>
  );
}

export type BannerTone = 'info' | 'caution' | 'error';
/** A page-level notice. Dismissible banners remember nothing; the caller decides how long to hide them. */
export function Banner({ tone = 'info', children, action, onDismiss }: { tone?: BannerTone; children: ReactNode; action?: ReactNode; onDismiss?: () => void }) {
  return (
    <div className={`ui-banner ui-banner-${tone}`} role={tone === 'error' ? 'alert' : 'status'}>
      <div className="ui-banner-body">{children}</div>
      {action}
      {onDismiss && <Button variant="ghost" size="sm" aria-label="Dismiss" onClick={onDismiss}>×</Button>}
    </div>
  );
}

/** Data older than expected, with its age. Renders nothing when fresh or when the as-of time is unknown (unknown is not "fresh": pass `unknownLabel`). */
export function StaleBadge({ asOf, staleAfterMinutes, now = Date.now(), unknownLabel }: { asOf: string | number | Date | null | undefined; staleAfterMinutes: number; now?: number; unknownLabel?: string }) {
  const t = asOf === null || asOf === undefined || asOf === '' ? NaN : new Date(asOf).getTime();
  if (Number.isNaN(t)) return unknownLabel ? <span className="ui-chip ui-tone-neutral" title="No as-of time was provided">{unknownLabel}</span> : null;
  const minutes = Math.max(0, Math.round((now - t) / 60000));
  if (minutes <= staleAfterMinutes) return null;
  const age = minutes < 60 ? `${minutes}m` : minutes < 2880 ? `${Math.round(minutes / 60)}h` : `${Math.round(minutes / 1440)}d`;
  return <span className="ui-chip ui-tone-caution" title={`As of ${formatIST(asOf)}`}><span aria-hidden="true" className="ui-chip-dot">▲</span>Stale {age}</span>;
}
