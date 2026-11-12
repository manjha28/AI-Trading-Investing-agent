import { useEffect, useId, useRef, useState } from 'react';

export type SelectOption = { value: string; label: string };

/** App-styled single select (button + listbox) so the popup follows the dark theme instead of the OS. Keyboard: Up/Down, Home/End, Enter/Space, Esc, type-ahead. */
export function StyledSelect({ value, options, onChange, ariaLabel, disabled = false }: {
  value: string; options: SelectOption[]; onChange: (value: string) => void; ariaLabel: string; disabled?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const root = useRef<HTMLDivElement>(null);
  const typed = useRef({ text: '', at: 0 });
  const listId = useId();
  const selected = options.find((o) => o.value === value);

  useEffect(() => {
    if (!open) return undefined;
    const close = (e: MouseEvent) => { if (!root.current?.contains(e.target as Node)) setOpen(false); };
    document.addEventListener('mousedown', close);
    return () => document.removeEventListener('mousedown', close);
  }, [open]);

  const commit = (i: number) => { const o = options[i]; if (o) onChange(o.value); setOpen(false); };
  const onKey = (e: React.KeyboardEvent) => {
    if (disabled) return;
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      if (!open) { setActive(Math.max(0, options.findIndex((o) => o.value === value))); setOpen(true); return; }
      setActive((a) => Math.min(options.length - 1, Math.max(0, a + (e.key === 'ArrowDown' ? 1 : -1))));
    } else if (e.key === 'Home' || e.key === 'End') { if (open) { e.preventDefault(); setActive(e.key === 'Home' ? 0 : options.length - 1); } }
    else if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); if (open) commit(active); else { setActive(Math.max(0, options.findIndex((o) => o.value === value))); setOpen(true); } }
    else if (e.key === 'Escape') { if (open) { e.preventDefault(); setOpen(false); } }
    else if (e.key.length === 1 && !e.ctrlKey && !e.metaKey) {
      const now = Date.now();
      typed.current = { text: (now - typed.current.at < 700 ? typed.current.text : '') + e.key.toLowerCase(), at: now };
      const i = options.findIndex((o) => o.label.toLowerCase().startsWith(typed.current.text));
      if (i >= 0) { setActive(i); if (!open) onChange(options[i].value); }
    }
  };

  return (
    <div className="sl-select" ref={root}>
      <button type="button" className="sl-select-btn" role="combobox" aria-label={ariaLabel} aria-haspopup="listbox" aria-expanded={open} aria-controls={listId} disabled={disabled}
        onClick={() => { setActive(Math.max(0, options.findIndex((o) => o.value === value))); setOpen(!open); }} onKeyDown={onKey}>
        <span>{selected?.label ?? '—'}</span><span aria-hidden="true">▾</span>
      </button>
      {open && (
        <ul className="sl-select-list" id={listId} role="listbox" aria-label={ariaLabel}>
          {options.map((o, i) => (
            <li key={o.value} role="option" aria-selected={o.value === value} className={`${i === active ? 'sl-active' : ''}${o.value === value ? ' sl-selected' : ''}`}
              onMouseEnter={() => setActive(i)} onMouseDown={(e) => { e.preventDefault(); commit(i); }}>{o.label}</li>
          ))}
        </ul>
      )}
    </div>
  );
}
