import type { ReactNode } from 'react';
import { STATUS_LABEL, toneOf, type Tone } from './tones';

const ICON: Record<Tone, string> = { positive: '●', negative: '●', caution: '▲', info: '●', neutral: '○' };

/** A status pill: fixed height, never wraps. Pass `status` (a domain word) for the standard tone, or `tone` for a one-off. */
export function Chip({ status, tone, children, title }: { status?: string; tone?: Tone; children?: ReactNode; title?: string }) {
  const t = tone ?? toneOf(status);
  const label = children ?? (status ? (STATUS_LABEL[status.toLowerCase().replace(/[\s-]+/g, '_')] ?? status) : null);
  return <span className={`ui-chip ui-tone-${t}`} title={title}><span aria-hidden="true" className="ui-chip-dot">{ICON[t]}</span>{label}</span>;
}
