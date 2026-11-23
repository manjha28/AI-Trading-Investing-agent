import { useEffect, useRef, type ReactNode } from 'react';

/** Search field. Press "/" anywhere (outside a text field) to focus it. */
export function SearchBox({ value, onChange, placeholder = 'Search', label = 'Search' }: { value: string; onChange: (v: string) => void; placeholder?: string; label?: string }) {
  const ref = useRef<HTMLInputElement>(null);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null;
      if (e.key !== '/' || e.metaKey || e.ctrlKey || e.altKey || t?.matches('input, textarea, select, [contenteditable="true"]')) return;
      e.preventDefault(); ref.current?.focus();
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, []);
  return <input ref={ref} type="search" className="ui-input" value={value} placeholder={`${placeholder} (press /)`} aria-label={label} onChange={(e) => onChange(e.target.value)} />;
}

export function Select({ label, value, onChange, options }: { label: string; value: string; onChange: (v: string) => void; options: { value: string; label: string }[] }) {
  return <label className="ui-field">{label}<select className="ui-input" value={value} onChange={(e) => onChange(e.target.value)}>{options.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}</select></label>;
}

export type FilterChip = { id: string; label: string; count?: number };
/** Toggle chips with counts, and one Clear all that appears only when something is on. */
export function FilterBar({ chips, active, onToggle, onClear, label = 'Filters' }: { chips: FilterChip[]; active: string[]; onToggle: (id: string) => void; onClear: () => void; label?: string }) {
  return (
    <div className="ui-filterbar" role="group" aria-label={label}>
      {chips.map((c) => <button key={c.id} type="button" className={`ui-fchip${active.includes(c.id) ? ' on' : ''}`} aria-pressed={active.includes(c.id)} onClick={() => onToggle(c.id)}>{c.label}{c.count !== undefined && <span>{c.count}</span>}</button>)}
      {active.length > 0 && <button type="button" className="ui-fclear" onClick={onClear}>Clear all</button>}
    </div>
  );
}

/** A short explanation on hover or keyboard focus; the text is also the accessible description. */
export function Tooltip({ text, children }: { text: string; children: ReactNode }) {
  const id = useRef(`tip-${Math.random().toString(36).slice(2, 8)}`).current;
  return <span className="ui-tip" aria-describedby={id} tabIndex={0}>{children}<span role="tooltip" id={id} className="ui-tip-body">{text}</span></span>;
}

export function Card({ title, actions, children }: { title?: string; actions?: ReactNode; children: ReactNode }) {
  return <section className="ui-card">{(title || actions) && <header><h3>{title}</h3>{actions}</header>}{children}</section>;
}
