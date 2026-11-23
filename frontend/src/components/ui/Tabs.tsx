import { useRef, type KeyboardEvent } from 'react';

export type TabItem = { id: string; label: string; count?: number };

/** Roving-tabindex tablist: Left/Right/Home/End move and select; only the active tab is in the tab order. */
export function Tabs({ items, active, onChange, label }: { items: TabItem[]; active: string; onChange: (id: string) => void; label: string }) {
  const refs = useRef<Record<string, HTMLButtonElement | null>>({});
  const move = (e: KeyboardEvent, index: number) => {
    const last = items.length - 1;
    const next = e.key === 'ArrowRight' ? (index === last ? 0 : index + 1) : e.key === 'ArrowLeft' ? (index === 0 ? last : index - 1) : e.key === 'Home' ? 0 : e.key === 'End' ? last : -1;
    if (next < 0) return;
    e.preventDefault();
    onChange(items[next].id);
    refs.current[items[next].id]?.focus();
  };
  return (
    <div role="tablist" aria-label={label} className="ui-tabs">
      {items.map((t, i) => (
        <button key={t.id} ref={(el) => { refs.current[t.id] = el; }} type="button" role="tab" id={`tab-${t.id}`} aria-selected={t.id === active} tabIndex={t.id === active ? 0 : -1}
          className={`ui-tab${t.id === active ? ' active' : ''}`} onClick={() => onChange(t.id)} onKeyDown={(e) => move(e, i)}>
          {t.label}{t.count !== undefined && <span className="ui-tab-count">{t.count}</span>}
        </button>
      ))}
    </div>
  );
}
