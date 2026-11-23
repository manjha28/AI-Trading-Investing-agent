/** A tiny trend line from a series. Unknown points (null) leave gaps; fewer than two known points draws nothing, never a flat line. */
export function Sparkline({ values, width = 80, height = 22, label = 'Trend' }: { values: (number | null | undefined)[]; width?: number; height?: number; label?: string }) {
  const known = values.filter((v): v is number => typeof v === 'number' && Number.isFinite(v));
  if (known.length < 2) return <span className="ui-muted" title="Not enough history">—</span>;
  const lo = Math.min(...known); const hi = Math.max(...known); const span = hi - lo || 1;
  const step = width / Math.max(values.length - 1, 1);
  const segments: string[] = []; let current = '';
  values.forEach((v, i) => {
    if (typeof v !== 'number' || !Number.isFinite(v)) { if (current) segments.push(current); current = ''; return; }
    current += `${current ? 'L' : 'M'}${(i * step).toFixed(1)},${(height - 2 - ((v - lo) / span) * (height - 4)).toFixed(1)}`;
  });
  if (current) segments.push(current);
  const up = known[known.length - 1] >= known[0];
  return <svg className={`ui-spark ui-tone-${up ? 'positive' : 'negative'}`} width={width} height={height} role="img" aria-label={`${label}: ${up ? 'up' : 'down'} from ${known[0]} to ${known[known.length - 1]}`}>{segments.map((d, i) => <path key={i} d={d} fill="none" stroke="currentColor" strokeWidth="1.5" />)}</svg>;
}

/** A 0-100 horizontal meter (for confidence). Unknown shows n/a, not an empty bar. */
export function Meter({ value, label }: { value: number | null | undefined; label: string }) {
  if (value === null || value === undefined || Number.isNaN(value)) return <span className="ui-score ui-score-na" aria-label={`${label}: not available`}>n/a</span>;
  const v = Math.max(0, Math.min(100, value));
  return <span className="ui-meter" role="meter" aria-label={label} aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(v)}><i className={`ui-tone-${v >= 70 ? "positive" : v >= 40 ? "caution" : "negative"}`} data-fill={Math.round(v)} ref={(el) => el?.style.setProperty("--meter", `${v}%`)} />
    <b>{Math.round(v)}</b></span>;
}
