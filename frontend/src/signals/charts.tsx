import { useEffect, useMemo, useRef, useState } from 'react';

type Pt = { t: string; v: number };

function scale(values: number[], pad = 0.05): [number, number] {
  const lo = Math.min(...values);
  const hi = Math.max(...values);
  const span = hi - lo || Math.abs(hi) || 1;
  return [lo - span * pad, hi + span * pad];
}

/** Interactive SVG line/area chart. Hover shows the nearest point. Width scales to the container (viewBox), so it never overflows. */
export function LineChart({ points, label, color = 'var(--accent)', fill = false, format = (v: number) => v.toLocaleString('en-IN', { maximumFractionDigits: 0 }), baseline, maxY }: {
  points: Pt[]; label: string; color?: string; fill?: boolean; format?: (v: number) => string; baseline?: number; maxY?: number;
}) {
  const [hover, setHover] = useState<number | null>(null);
  const box = useRef<HTMLElement>(null);
  const [W, setW] = useState(640);
  const H = 180;
  useEffect(() => {
    const el = box.current;
    if (!el || typeof ResizeObserver === 'undefined') return undefined;
    const ro = new ResizeObserver((entries) => { const w = Math.round(entries[0].contentRect.width); if (w > 120) setW(w); });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  const L = 52;
  const R = 8;
  const T = 8;
  const B = 18;
  const geom = useMemo(() => {
    if (points.length < 2) return null;
    const [lo, rawHi] = scale(points.map((p) => p.v).concat(baseline !== undefined ? [baseline] : []));
    const hi = maxY !== undefined ? maxY : rawHi;
    const x = (i: number) => L + (i / (points.length - 1)) * (W - L - R);
    const y = (v: number) => T + (1 - (v - lo) / (hi - lo)) * (H - T - B);
    const d = points.map((p, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(p.v).toFixed(1)}`).join('');
    return { x, y, d, lo, hi };
  }, [points, baseline, maxY, W]);
  if (!geom) return <p className="sl-muted">Not enough points to chart.</p>;
  const h = hover === null ? null : points[hover];
  return (
    <figure className="sl-chart" ref={box}>
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={label}
        onMouseLeave={() => setHover(null)}
        onMouseMove={(e) => {
          const r = e.currentTarget.getBoundingClientRect();
          const rel = ((e.clientX - r.left) / r.width) * W;
          setHover(Math.max(0, Math.min(points.length - 1, Math.round(((rel - L) / (W - L - R)) * (points.length - 1)))));
        }}>
        {[0, 0.5, 1].map((f) => {
          const v = geom.lo + (geom.hi - geom.lo) * f;
          return <g key={f}><line x1={L} x2={W - R} y1={geom.y(v)} y2={geom.y(v)} className="sl-grid" /><text x={L - 4} y={geom.y(v) + 3} textAnchor="end" className="sl-axis">{format(v)}</text></g>;
        })}
        {baseline !== undefined && <line x1={L} x2={W - R} y1={geom.y(baseline)} y2={geom.y(baseline)} className="sl-baseline" />}
        {fill && <path d={`${geom.d}L${geom.x(points.length - 1)},${geom.y(geom.lo)}L${geom.x(0)},${geom.y(geom.lo)}Z`} fill={color} opacity="0.12" />}
        <path d={geom.d} fill="none" stroke={color} strokeWidth="1.5" />
        {hover !== null && h && <g><line x1={geom.x(hover)} x2={geom.x(hover)} y1={T} y2={H - B} className="sl-cross" /><circle cx={geom.x(hover)} cy={geom.y(h.v)} r="3" fill={color} /></g>}
      </svg>
      <figcaption className="sl-chart-read" aria-live="polite">{h ? `${new Date(h.t).toLocaleString('en-IN', { dateStyle: 'medium', timeStyle: 'short', timeZone: 'Asia/Kolkata' })} · ${format(h.v)}` : label}</figcaption>
    </figure>
  );
}

export function BarChart({ bars, label, format = (v: number) => v.toFixed(2) }: { bars: { key: string; v: number }[]; label: string; format?: (v: number) => string }) {
  const [hover, setHover] = useState<string | null>(null);
  if (!bars.length) return <p className="sl-muted">No closed trades to chart.</p>;
  const max = Math.max(...bars.map((b) => Math.abs(b.v)), 1e-9);
  return (
    <figure className="sl-chart">
      <div className="sl-bars" role="img" aria-label={label}>
        {bars.map((b) => (
          <div key={b.key} className="sl-bar-col" onMouseEnter={() => setHover(b.key)} onMouseLeave={() => setHover(null)} title={`${b.key}: ${format(b.v)}`}>
            <div className="sl-bar-track"><div className={`sl-bar ${b.v >= 0 ? 'sl-bar-pos' : 'sl-bar-neg'}`} style={{ height: `${Math.max(2, (Math.abs(b.v) / max) * 100)}%` }} /></div>
            <span>{b.key.slice(2)}</span>
          </div>
        ))}
      </div>
      <figcaption className="sl-chart-read">{hover ? `${hover} · ${format(bars.find((b) => b.key === hover)?.v ?? 0)}` : label}</figcaption>
    </figure>
  );
}
