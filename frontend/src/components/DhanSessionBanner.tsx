import { useEffect, useState } from 'react';
import { apiRequest } from '../api';
import { formatIST } from '../lib/formatIST';
import { sessionBanner } from '../lib/sessionExpiry';

type BrokerRow = { slug: string; status: string; expires_at: string | null };

/** The Dhan session expiry on Overview, with a warning when it is expired or under 60 minutes away and a link to the Dhan connection page. Fetches once the session is allowed to read broker data. */
export function DhanSessionBanner({ allowed, onOpenBrokers, now }: { allowed: boolean; onOpenBrokers: () => void; now?: number }) {
  const [expiresAt, setExpiresAt] = useState<string | null | undefined>(undefined);
  const [clock, setClock] = useState(() => now ?? Date.now());
  useEffect(() => {
    if (!allowed) return undefined;
    let active = true;
    void apiRequest<BrokerRow[]>('/brokers').then((rows) => { if (active) setExpiresAt(rows.find((b) => b.slug === 'dhan')?.expires_at ?? null); }).catch(() => { if (active) setExpiresAt(null); });
    return () => { active = false; };
  }, [allowed]);
  useEffect(() => {
    if (now !== undefined) return undefined;
    const t = window.setInterval(() => setClock(Date.now()), 30_000);
    return () => window.clearInterval(t);
  }, [now]);
  if (!allowed || expiresAt === undefined) return null;
  const banner = sessionBanner(expiresAt, clock);
  return (
    <>
      <p className="broker-sync-time" data-testid="dhan-session-expiry">Dhan session: {expiresAt ? `expires ${formatIST(expiresAt)}` : 'expiry not reported'}</p>
      {banner && (
        <div className={`session-banner session-banner-${banner.level}`} role="alert">
          <span>{banner.text}</span>
          <button className="secondary-button" type="button" onClick={onOpenBrokers}>Open Dhan connection</button>
        </div>
      )}
    </>
  );
}
