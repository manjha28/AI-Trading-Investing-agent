import { useEffect, useState } from 'react';
import { apiRequest } from '../api';
import { Chip } from '../components/ui/kit';

type Status = { deployment_enabled: boolean; armed: boolean };

/** States the REAL order capability from the server's config, so a paper test is never mistaken for a live one. */
export function LiveCapabilityChip() {
  const [s, setS] = useState<Status | null | undefined>(undefined);
  useEffect(() => { let on = true; apiRequest<Status>('/dhan-live/status').then((r) => on && setS(r)).catch(() => on && setS(null)); return () => { on = false; }; }, []);
  if (s === undefined) return null;
  if (s === null) return <Chip tone="neutral" title="The server did not report its order capability">Order capability unknown</Chip>;
  if (!s.deployment_enabled) return <Chip tone="info" title="Dhan live execution is switched off on this deployment: every Forward Test here is paper only">Paper only: Dhan live orders off</Chip>;
  return s.armed ? <Chip tone="caution" title="Armed: a test with Live switched on can place a real 1-share intraday order">Live orders ARMED (Dhan)</Chip> : <Chip tone="info" title="Live execution is available but not armed by you">Paper only: Dhan live available, not armed</Chip>;
}
