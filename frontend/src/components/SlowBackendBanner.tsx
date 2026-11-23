import { useEffect, useState } from 'react';
import { subscribeSlowRequests } from '../api';
import { Banner } from './ui/kit';

/** Shown while any request has been pending for more than 8 seconds: the page is not frozen, the backend is slow. */
export function SlowBackendBanner() {
  const [slow, setSlow] = useState(0);
  useEffect(() => subscribeSlowRequests(setSlow), []);
  if (slow === 0) return null;
  return <Banner tone="caution">Backend is slow. Still loading {slow === 1 ? 'one request' : `${slow} requests`}; nothing is lost.</Banner>;
}
