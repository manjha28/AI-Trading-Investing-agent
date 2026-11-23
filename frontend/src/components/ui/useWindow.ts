import { useCallback, useEffect, useState } from 'react';

/** Windowing for long tables: only the rows near the viewport are rendered (no dependency). `rowHeight` is fixed per density. */
export function windowRange(total: number, scrollTop: number, viewport: number, rowHeight: number, overscan = 8): { start: number; end: number } {
  const start = Math.max(0, Math.floor(scrollTop / rowHeight) - overscan);
  const end = Math.min(total, Math.ceil((scrollTop + viewport) / rowHeight) + overscan);
  return { start, end };
}

export function useWindow(total: number, rowHeight: number, viewport: number, enabled: boolean) {
  const [scrollTop, setScrollTop] = useState(0);
  const onScroll = useCallback((e: { currentTarget: { scrollTop: number } }) => setScrollTop(e.currentTarget.scrollTop), []);
  useEffect(() => { setScrollTop(0); }, [total]);
  if (!enabled) return { start: 0, end: total, onScroll, padTop: 0, padBottom: 0 };
  const { start, end } = windowRange(total, scrollTop, viewport, rowHeight);
  return { start, end, onScroll, padTop: start * rowHeight, padBottom: (total - end) * rowHeight };
}

export type Density = 'comfortable' | 'compact';
/** Comfortable (default) or compact rows, remembered in local storage. */
export function useDensity(): [Density, (d: Density) => void] {
  const [density, setDensity] = useState<Density>(() => { try { return window.localStorage.getItem('ui.density') === 'compact' ? 'compact' : 'comfortable'; } catch { return 'comfortable'; } });
  const set = useCallback((d: Density) => { setDensity(d); try { window.localStorage.setItem('ui.density', d); } catch { /* per-viewer convenience */ } }, []);
  return [density, set];
}
