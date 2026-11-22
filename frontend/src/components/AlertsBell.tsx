import { useCallback, useEffect, useRef, useState } from 'react';
import { BellRing } from 'lucide-react';
import { apiRequest } from '../api';
import { ruleLabel, type AlertEvent } from './StockAlerts';
import { formatIST } from '../lib/formatIST';

const POLL_MS = 60_000;
const when = (iso: string) => formatIST(iso);

function summary(e: AlertEvent): string {
  const ev = e.evidence ?? {};
  if (e.rule_type === 'research_buy') return `${(Array.isArray(ev.horizons) ? ev.horizons : []).map((h: string) => h.replace('_', '-')).join(', ')} · confidence ${ev.confidence ?? '—'}${ev.confidence_band ? ` (${ev.confidence_band})` : ''}`;
  const bits = ['symbol', 'price', 'last_price', 'threshold', 'threshold_pct', 'move_pct', 'rsi'].filter((k) => ev[k] !== undefined && ev[k] !== null).slice(0, 3).map((k) => `${k.replace(/_/g, ' ')} ${String(ev[k])}`);
  return bits.join(' · ');
}

/** Header bell: new alerts across all stocks. Click an alert to jump to that stock in Market watch. */
export function AlertsBell({ onOpenStock, onOpenResearch, refreshKey = 0 }: { onOpenStock: (instrumentId: string) => void; onOpenResearch?: (instrumentId: string) => void; refreshKey?: number }) {
  const [events, setEvents] = useState<AlertEvent[]>([]);
  const [open, setOpen] = useState(false);
  const [error, setError] = useState(false);
  const [symbolById, setSymbolById] = useState<Record<string, string>>({});
  const box = useRef<HTMLDivElement>(null);

  const load = useCallback(() => apiRequest<AlertEvent[]>('/alerts/events?status=new&limit=30').then((r) => { setEvents(r); setError(false); }).catch(() => setError(true)), []);
  useEffect(() => { void load(); const t = window.setInterval(() => void load(), POLL_MS); return () => window.clearInterval(t); }, [load, refreshKey]);
  // names for the stocks the alerts are about (looked up once each)
  useEffect(() => {
    const unknown = [...new Set(events.map((e) => e.instrument_id).filter((id): id is string => Boolean(id) && !(id as string in symbolById)))];
    unknown.forEach((id) => { apiRequest<{ symbol: string }>(`/market-data/instruments/${id}`).then((i) => setSymbolById((cur) => ({ ...cur, [id]: i.symbol }))).catch(() => setSymbolById((cur) => ({ ...cur, [id]: 'Stock' }))); });
  }, [events]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (!open) return undefined;
    const close = (e: MouseEvent) => { if (box.current && !box.current.contains(e.target as Node)) setOpen(false); };
    const esc = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false); };
    document.addEventListener('mousedown', close); document.addEventListener('keydown', esc);
    return () => { document.removeEventListener('mousedown', close); document.removeEventListener('keydown', esc); };
  }, [open]);

  const markRead = async (ids: string[]) => {
    await Promise.all(ids.map((id) => apiRequest(`/alerts/events/${id}`, { method: 'PATCH', body: JSON.stringify({ status: 'read' }) }).catch(() => undefined)));
    await load();
  };
  const count = events.length;
  return (
    <div className="alerts-bell" ref={box}>
      <button type="button" className="bell-button" aria-label={count ? `Alerts: ${count} new` : 'Alerts: none new'} aria-expanded={open} onClick={() => setOpen((o) => !o)}>
        <BellRing size={17} />{count > 0 && <span className="bell-badge" aria-hidden="true">{count > 9 ? '9+' : count}</span>}
      </button>
      {open && (
        <div className="bell-menu" role="dialog" aria-label="New alerts">
          <div className="bell-head"><strong>Alerts</strong>{count > 0 && <button type="button" className="text-button" onClick={() => void markRead(events.map((e) => e.id))}>Mark all read</button>}</div>
          {error && <p className="crypto-sub">Alerts could not be loaded.</p>}
          {!error && count === 0 && <p className="crypto-sub">Nothing new. Add an alert from any stock in Market watch.</p>}
          <ul>
            {events.map((e) => (
              <li key={e.id}>
                <button type="button" className="bell-item" onClick={() => { setOpen(false); if (e.instrument_id) { if (e.rule_type === 'research_buy' && onOpenResearch) onOpenResearch(e.instrument_id); else onOpenStock(e.instrument_id); } void markRead([e.id]); }}>
                  <span><strong>{e.instrument_id ? symbolById[e.instrument_id] ?? 'Stock' : 'Portfolio'}</strong> · {e.rule_type === 'research_buy' ? 'Ready to buy' : ruleLabel(e.rule_type)}</span>
                  <small>{when(e.fired_at)}{summary(e) ? ` · ${summary(e)}` : ''}</small>
                </button>
              </li>))}
          </ul>
        </div>
      )}
    </div>
  );
}
