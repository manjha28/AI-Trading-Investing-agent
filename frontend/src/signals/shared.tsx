import { useEffect, useRef, type ReactNode } from 'react';

export function Scroll({ children, label }: { children: ReactNode; label?: string }) {
  return <div className="sl-scroll" role="region" aria-label={label} tabIndex={0}>{children}</div>;
}

export function Pill({ tone = 'neutral', children, title }: { tone?: 'ok' | 'warn' | 'bad' | 'info' | 'neutral'; children: ReactNode; title?: string }) {
  return <span className={`sl-pill sl-pill-${tone}`} title={title}>{children}</span>;
}

export function statusTone(status: string): 'ok' | 'warn' | 'bad' | 'info' | 'neutral' {
  if (['active', 'ok', 'completed', 'new', 'full', 'supported'].includes(status)) return 'ok';
  if (['warn', 'partial', 'suppressed', 'running', 'queued', 'suspended'].includes(status)) return 'warn';
  if (['refuse', 'failed', 'unsupported', 'no_data', 'retired', 'invalidated', 'no_usable_data', 'order_flow_unavailable', 'unavailable'].includes(status)) return 'bad';
  return 'neutral';
}

export function StatusPill({ status }: { status: string }) {
  return <Pill tone={statusTone(status)}>{status.replace('_', ' ')}</Pill>;
}

export function Notice({ tone = 'info', children }: { tone?: 'info' | 'warn' | 'bad' | 'ok'; children: ReactNode }) {
  return <div className={`sl-notice sl-notice-${tone}`} role={tone === 'bad' ? 'alert' : 'status'}>{children}</div>;
}

export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return <div className="sl-empty"><strong>{title}</strong>{children && <p>{children}</p>}</div>;
}

export function Stat({ label, value, tone, hint }: { label: string; value: ReactNode; tone?: 'pos' | 'neg'; hint?: string }) {
  return <div className="sl-stat" title={hint}><span>{label}</span><strong className={tone ? `sl-${tone}` : ''}>{value}</strong></div>;
}

export function ConfirmDialog({ title, body, confirmLabel, onConfirm, onCancel, danger }: {
  title: string; body: ReactNode; confirmLabel: string; onConfirm: () => void; onCancel: () => void; danger?: boolean;
}) {
  const ref = useRef<HTMLButtonElement>(null);
  useEffect(() => { ref.current?.focus(); }, []);
  return (
    <div className="sl-modal-backdrop" onKeyDown={(e) => { if (e.key === 'Escape') onCancel(); }}>
      <div className="sl-modal" role="dialog" aria-modal="true" aria-label={title}>
        <h3>{title}</h3>
        <div className="sl-modal-body">{body}</div>
        <div className="sl-modal-actions">
          <button type="button" onClick={onCancel}>Cancel</button>
          <button type="button" ref={ref} className={danger ? 'sl-danger' : 'sl-primary'} onClick={onConfirm}>{confirmLabel}</button>
        </div>
      </div>
    </div>
  );
}

export function Field({ label, children, hint }: { label: string; children: ReactNode; hint?: string }) {
  return <label className="sl-field"><span>{label}</span>{children}{hint && <small>{hint}</small>}</label>;
}
