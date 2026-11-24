import { useState } from 'react';

export type AttentionItem = { id: string; tone: 'negative' | 'caution' | 'info'; text: string; count?: number; action?: { label: string; onClick: () => void } };

const ICON = { negative: '●', caution: '▲', info: 'ℹ' } as const;
const MAX_ROWS = 5;

/** A quiet list in the Multibagger style: one row per item (icon, short text, count, link), five rows then "Show all".
 *  Nothing is rendered when nothing needs attention or while it is still being worked out. */
export function NeedsAttention({ items, loading }: { items: AttentionItem[]; loading?: boolean }) {
  const [all, setAll] = useState(false);
  if (loading || items.length === 0) return null;
  const shown = all ? items : items.slice(0, MAX_ROWS);
  return (
    <section className="needs-attention" aria-label="Needs attention">
      <h2>Needs attention <span className="na-total">{items.length}</span></h2>
      <ul>
        {shown.map((i) => (
          <li key={i.id} className={`na-${i.tone}`}>
            <span className="na-icon" aria-label={i.tone === 'negative' ? 'Act' : i.tone === 'caution' ? 'Check' : 'Info'}>{ICON[i.tone]}</span>
            <span className="na-text">{i.text}</span>
            {i.count !== undefined && <span className="na-count">{i.count}</span>}
            {i.action && <button type="button" className="na-link" onClick={i.action.onClick}>{i.action.label}</button>}
          </li>
        ))}
      </ul>
      {items.length > MAX_ROWS && <button type="button" className="na-link na-more" onClick={() => setAll((v) => !v)}>{all ? 'Show fewer' : `Show all ${items.length}`}</button>}
    </section>
  );
}
