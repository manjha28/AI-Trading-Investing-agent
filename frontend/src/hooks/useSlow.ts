import { useEffect, useState } from 'react';

/** True once ``active`` has stayed true for ``ms`` (default 10 s): lets a loading placeholder say "Still loading (slow backend)" instead of giving up with "No data". Resets when ``active`` turns false. */
export function useSlow(active: boolean, ms = 10_000): boolean {
  const [slow, setSlow] = useState(false);
  useEffect(() => {
    if (!active) { setSlow(false); return undefined; }
    const timer = window.setTimeout(() => setSlow(true), ms);
    return () => window.clearTimeout(timer);
  }, [active, ms]);
  return slow;
}
