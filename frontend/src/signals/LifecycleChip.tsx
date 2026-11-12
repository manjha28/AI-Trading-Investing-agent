import { GROUP_LABEL, STATE_META, isLifecycleState } from './lifecycle';

export function LifecycleChip({ status }: { status: string | null | undefined }) {
  if (typeof status !== 'string') return <span className="sl-muted">—</span>;
  if (!isLifecycleState(status)) return <span className="sl-pill sl-state sl-state-unknown" title="State not recognised by this view">{status.replace('_', ' ')}</span>;
  const m = STATE_META[status];
  return (
    <span className={`sl-pill sl-state sl-state-${m.group} sl-state-${status}`} title={`${m.label}: ${GROUP_LABEL[m.group]}. Colour shows lifecycle stage, not whether the outcome was favourable.`} data-state={status} data-group={m.group}>
      <span aria-hidden="true">{m.glyph} </span>{m.label}
    </span>
  );
}
