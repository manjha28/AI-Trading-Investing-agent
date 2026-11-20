const num = (value: string | null | undefined): number | null => {
  if (value === null || value === undefined || value === '') return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
};
const fmt = (value: number | null) => (value === null ? '—' : value.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 }));

/** Day low — marker showing where the last price sits — day high. Unknown values render as "—", never zero. */
export function DayRange({ low, high, last }: { low: string | null | undefined; high: string | null | undefined; last: string | null | undefined }) {
  const lo = num(low), hi = num(high), px = num(last);
  const position = lo !== null && hi !== null && px !== null && hi > lo ? Math.max(0, Math.min(100, ((px - lo) / (hi - lo)) * 100)) : null;
  return (
    <>
      <span>{fmt(lo)}</span>
      <div className="watch-range-bar"><i style={{ left: `${position ?? 50}%`, visibility: position === null ? 'hidden' : 'visible' }} /></div>
      <span>{fmt(hi)}</span>
    </>
  );
}
