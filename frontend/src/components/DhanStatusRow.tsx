import { useEffect, useState } from 'react';
import { apiRequest } from '../api';

type BrokerRow = { slug: string; status: string; expires_at: string | null };
const label = (minutes: number): string => (minutes >= 1440 ? `${Math.floor(minutes / 1440)}d` : minutes >= 60 ? `${Math.floor(minutes / 60)}h` : `${minutes}m`);

/** Compact sidebar status: green dot with the time left, amber under 60 minutes, red when expired or not connected. Opens the Dhan connection page. */
export function DhanStatusRow({ allowed, onOpen, now }: { allowed: boolean; onOpen: () => void; now?: number }) {
  const [row, setRow] = useState<BrokerRow | null | undefined>(undefined);
  const [clock, setClock] = useState(() => now ?? Date.now());
  useEffect(() => {
    if (!allowed) return undefined;
    let active = true;
    const load = () => void apiRequest<BrokerRow[]>('/brokers').then((rows) => { if (active) setRow(rows.find((b) => b.slug === 'dhan') ?? null); }).catch(() => { if (active) setRow(null); });
    load();
    const t = window.setInterval(() => { if (now === undefined) setClock(Date.now()); load(); }, 60_000);
    return () => { active = false; window.clearInterval(t); };
  }, [allowed, now]);
  if (!allowed || row === undefined) return null;
  const expires = row?.expires_at ? new Date(row.expires_at).getTime() : NaN;
  const minutes = Number.isNaN(expires) ? null : Math.floor((expires - clock) / 60000);
  const state = !row || row.status === 'not_connected' ? 'off' : minutes !== null && minutes <= 0 ? 'bad' : minutes !== null && minutes < 60 ? 'warn' : minutes === null ? 'unknown' : 'ok';
  const text = state === 'off' ? 'Dhan · not connected' : state === 'bad' ? 'Dhan · session expired' : state === 'unknown' ? 'Dhan · expiry unknown' : `Dhan · expires in ${label(minutes ?? 0)}`;
  return (
    <button type="button" className={`dhan-status dhan-status-${state}`} onClick={onOpen} title="Open the Dhan connection page">
      <span aria-hidden="true" className="dhan-status-dot" />{text}
    </button>
  );
}
