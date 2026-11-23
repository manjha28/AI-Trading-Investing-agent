import { useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from 'react';
import { EmptyState, ErrorState, Skeleton } from './States';
import { useDensity, useWindow, type Density } from './useWindow';

export type Column<T> = {
  key: string; header: string; render: (row: T) => ReactNode;
  sortValue?: (row: T) => number | string | null | undefined;     // present = the header sorts; unknown (null/NaN) always sorts last
  align?: 'left' | 'right'; firstSort?: 'asc' | 'desc'; sticky?: boolean; hideable?: boolean; defaultHidden?: boolean; title?: string;
};
export type SortState = { key: string; dir: 1 | -1 };
const ROW_HEIGHT: Record<Density, number> = { comfortable: 40, compact: 32 };
export const VIRTUALISE_ABOVE = 100;

const unknown = (v: unknown) => v === null || v === undefined || (typeof v === 'number' && Number.isNaN(v)) || v === '';

/** Sorted, windowed, keyboard-friendly table. Scrolls sideways INSIDE its own container (never the page) and renders only the visible rows above 100. */
export function DataTable<T>({ columns, rows, rowKey, caption, onRowClick, selectedKey, defaultSort, viewportHeight = 560, virtualiseAbove = VIRTUALISE_ABOVE, loading, error, onRetry, empty, density: densityProp }: {
  columns: Column<T>[]; rows: T[]; rowKey: (row: T) => string; caption: string; onRowClick?: (row: T) => void; selectedKey?: string | null; defaultSort?: SortState;
  viewportHeight?: number; virtualiseAbove?: number; loading?: boolean; error?: unknown; onRetry?: () => void; empty?: ReactNode; density?: Density;
}) {
  const [stored] = useDensity();
  const density = densityProp ?? stored;
  const rowHeight = ROW_HEIGHT[density];
  const [sort, setSort] = useState<SortState | undefined>(defaultSort);
  const [hidden, setHidden] = useState<Set<string>>(() => new Set(columns.filter((c) => c.defaultHidden).map((c) => c.key)));
  const wrap = useRef<HTMLDivElement>(null);

  const visible = columns.filter((c) => !hidden.has(c.key));
  const sorted = useMemo(() => {
    const col = sort ? columns.find((c) => c.key === sort.key) : undefined;
    if (!col?.sortValue || !sort) return rows;
    const get = col.sortValue;
    return rows.map((r, i) => ({ r, i, v: get(r) })).sort((a, b) => {
      const ua = unknown(a.v); const ub = unknown(b.v);
      if (ua || ub) return ua && ub ? a.i - b.i : ua ? 1 : -1;       // unknown last, whichever direction
      const c = typeof a.v === 'number' && typeof b.v === 'number' ? a.v - b.v : String(a.v).localeCompare(String(b.v), 'en', { numeric: true });
      return c !== 0 ? c * sort.dir : a.i - b.i;
    }).map((x) => x.r);
  }, [rows, columns, sort]);

  const windowed = sorted.length > virtualiseAbove;
  const win = useWindow(sorted.length, rowHeight, viewportHeight, windowed);
  const shown = sorted.slice(win.start, win.end);

  if (error) return <ErrorState error={error} onRetry={onRetry} context={caption} />;
  if (loading) return <Skeleton variant="table" />;
  if (rows.length === 0) return <EmptyState>{empty ?? 'Nothing to show yet.'}</EmptyState>;

  const toggleSort = (c: Column<T>) => c.sortValue && setSort((s) => (s?.key === c.key ? { key: c.key, dir: (s.dir * -1) as 1 | -1 } : { key: c.key, dir: c.firstSort ? (c.firstSort === 'desc' ? -1 : 1) : c.align === 'right' ? -1 : 1 }));
  const onKeyDown = (e: KeyboardEvent<HTMLTableRowElement>, row: T) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onRowClick?.(row); return; }
    const dir = e.key === 'ArrowDown' ? 1 : e.key === 'ArrowUp' ? -1 : 0;
    if (!dir) return;
    e.preventDefault();
    const sibling = (dir === 1 ? e.currentTarget.nextElementSibling : e.currentTarget.previousElementSibling) as HTMLElement | null;
    if (sibling?.tabIndex !== undefined && sibling.matches('tr[data-row]')) sibling.focus();
    else if (wrap.current) wrap.current.scrollTop += dir * rowHeight;
  };

  return (
    <div className="ui-table">
      {columns.some((c) => c.hideable) && (
        <details className="ui-colmenu"><summary>Columns</summary>
          <div>{columns.filter((c) => c.hideable).map((c) => (
            <label key={c.key}><input type="checkbox" checked={!hidden.has(c.key)} onChange={() => setHidden((h) => { const n = new Set(h); if (n.has(c.key)) n.delete(c.key); else n.add(c.key); return n; })} /> {c.header}</label>
          ))}</div>
        </details>
      )}
      <div ref={wrap} className={`ui-table-wrap ui-density-${density}`} style={windowed ? { maxHeight: viewportHeight } : undefined} onScroll={windowed ? win.onScroll : undefined}>
        <table aria-label={caption} aria-rowcount={sorted.length}>
          <thead><tr>{visible.map((c) => (
            <th key={c.key} scope="col" title={c.title} className={`${c.align === 'right' ? 'ui-num' : ''}${c.sticky ? ' ui-sticky' : ''}`}
              aria-sort={sort?.key === c.key ? (sort.dir === 1 ? 'ascending' : 'descending') : c.sortValue ? 'none' : undefined}>
              {c.sortValue ? <button type="button" onClick={() => toggleSort(c)}>{c.header}{sort?.key === c.key ? (sort.dir === 1 ? ' ▲' : ' ▼') : ''}</button> : c.header}
            </th>))}</tr></thead>
          <tbody>
            {win.padTop > 0 && <tr aria-hidden="true" style={{ height: win.padTop }}><td colSpan={visible.length} /></tr>}
            {shown.map((row, i) => {
              const key = rowKey(row);
              return (
                <tr key={key} data-row="" tabIndex={i === 0 ? 0 : -1} aria-rowindex={win.start + i + 2} aria-selected={selectedKey === key || undefined}
                  className={`${selectedKey === key ? 'selected' : ''}${onRowClick ? ' clickable' : ''}`} onClick={onRowClick ? () => onRowClick(row) : undefined} onKeyDown={(e) => onKeyDown(e, row)}>
                  {visible.map((c) => <td key={c.key} className={`${c.align === 'right' ? 'ui-num' : ''}${c.sticky ? ' ui-sticky' : ''}`}>{c.render(row)}</td>)}
                </tr>
              );
            })}
            {win.padBottom > 0 && <tr aria-hidden="true" style={{ height: win.padBottom }}><td colSpan={visible.length} /></tr>}
          </tbody>
        </table>
      </div>
    </div>
  );
}
