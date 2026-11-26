import { Search } from 'lucide-react';
import { useEffect, useId, useState } from 'react';
import { apiRequest } from '../api';

type Hit = { id: string; symbol: string; display_name?: string; venue?: string };

/** The top search: shows matching stocks while typing; choosing one (or pressing Enter) opens it in Market watch. */
export function HeaderSearch({ value, onChange, onSearch }: { value: string; onChange: (v: string) => void; onSearch: (term: string) => void }) {
  const [hits, setHits] = useState<Hit[] | null | undefined>(null);
  const [open, setOpen] = useState(false);
  const listId = useId();
  useEffect(() => {
    const term = value.trim();
    if (!open || term.length < 2) { setHits(null); return undefined; }
    let live = true;
    const timer = window.setTimeout(() => {
      apiRequest<Hit[]>(`/market-data/instruments?${new URLSearchParams({ query: term, limit: '6' })}`)
        .then((items) => { if (live) setHits(Array.isArray(items) ? items : []); })
        .catch(() => { if (live) setHits(undefined); });
    }, 300);
    return () => { live = false; window.clearTimeout(timer); };
  }, [value, open]);
  const go = (term: string) => { setOpen(false); onChange(term); onSearch(term); };
  return (
    <form className="header-search" role="search" onSubmit={(event) => { event.preventDefault(); go(value.trim()); }} onBlur={(e) => { if (!e.currentTarget.contains(e.relatedTarget as Node)) setOpen(false); }}>
      <Search size={15} aria-hidden="true" />
      <input type="search" aria-label="Search instruments" placeholder="Search a stock, then press Enter" autoComplete="off" aria-controls={listId} value={value}
        onChange={(event) => { onChange(event.target.value); setOpen(true); }} onKeyDown={(e) => { if (e.key === 'Escape') setOpen(false); }} />
      {open && hits === undefined && value.trim().length >= 2 && <p className="header-suggest header-suggest-none" id={listId}>Suggestions are not available right now. Press Enter to search in Market watch.</p>}
      {open && hits && value.trim().length >= 2 && (
        <ul className="header-suggest" id={listId} aria-label="Matching stocks">
          {hits.map((h) => <li key={h.id}><button type="button" onClick={() => go(h.symbol)}><strong>{h.symbol}</strong><small>{h.display_name && h.display_name !== h.symbol ? h.display_name : h.venue}</small></button></li>)}
          {hits.length === 0 && <li className="header-suggest-none">No stock matches “{value.trim()}”.</li>}
        </ul>
      )}
    </form>
  );
}
