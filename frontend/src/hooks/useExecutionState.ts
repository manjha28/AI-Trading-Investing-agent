import { useEffect, useState } from 'react';
import { apiRequest } from '../api';
import type { ExecutionStates } from '../lib/executionState';

// One shared poll for every component on the page; each screen reads the same object.
let current: ExecutionStates | null = null;
let timer: number | undefined;
const listeners = new Set<(s: ExecutionStates | null) => void>();

async function refresh(): Promise<void> {
  try {
    const next = await apiRequest<ExecutionStates>('/execution/state');
    if (next && next.dhan?.effective && next.delta?.effective) current = next;       // an unreadable reply never becomes a state
  } catch { current = current ?? null; }
  listeners.forEach((l) => l(current));
}
export function refreshExecutionState(): Promise<void> { return refresh(); }

/** The effective execution state of both venues, refreshed every 30 seconds while any component uses it. */
export function useExecutionState(enabled = true): ExecutionStates | null {
  const [state, setState] = useState<ExecutionStates | null>(current);
  useEffect(() => {
    if (!enabled) return undefined;
    listeners.add(setState);
    if (listeners.size === 1) { void refresh(); timer = window.setInterval(() => void refresh(), 30_000); } else setState(current);
    return () => { listeners.delete(setState); if (listeners.size === 0 && timer) { window.clearInterval(timer); timer = undefined; } };
  }, [enabled]);
  return state;
}
