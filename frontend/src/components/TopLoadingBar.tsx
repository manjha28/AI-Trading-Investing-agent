import { useEffect, useState } from 'react';
import { subscribePending } from '../api';

/** A thin bar under the header while requests are in flight. It appears only after 400 ms, so quick calls never flicker, and it has no motion under prefers-reduced-motion. */
export function TopLoadingBar() {
  const [pending, setPending] = useState(0);
  const [visible, setVisible] = useState(false);
  useEffect(() => subscribePending(setPending), []);
  useEffect(() => {
    if (pending === 0) { setVisible(false); return undefined; }
    const timer = window.setTimeout(() => setVisible(true), 400);
    return () => window.clearTimeout(timer);
  }, [pending]);
  return visible ? <div className="top-loading-bar" role="progressbar" aria-label="Loading" aria-busy="true"><i /></div> : null;
}
