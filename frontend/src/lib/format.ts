/** The app's number formats (Indian conventions). Unknown values render as the fallback, never as 0. The Trade journal keeps its own formatters. */
export { formatIST, formatISTDate } from './formatIST';

const finite = (v: number | string | null | undefined): number | null => {
  if (v === null || v === undefined || v === '') return null;
  const n = typeof v === 'number' ? v : Number(v);
  return Number.isFinite(n) ? n : null;
};

/** ₹1,23,456.00 (lakh/crore grouping). */
export function formatINR(value: number | string | null | undefined, opts: { decimals?: number; fallback?: string } = {}): string {
  const n = finite(value);
  if (n === null) return opts.fallback ?? '—';
  const d = opts.decimals ?? 2;
  return `${n < 0 ? '-' : ''}₹${Math.abs(n).toLocaleString('en-IN', { minimumFractionDigits: d, maximumFractionDigits: d })}`;
}

/** Volumes and large amounts as K / L / Cr: 12.3K, 4.5L, 1.20Cr. */
export function formatCompact(value: number | string | null | undefined, fallback = '—'): string {
  const n = finite(value);
  if (n === null) return fallback;
  const a = Math.abs(n); const sign = n < 0 ? '-' : '';
  if (a >= 1e7) return `${sign}${(a / 1e7).toFixed(2)}Cr`;
  if (a >= 1e5) return `${sign}${(a / 1e5).toFixed(2)}L`;
  if (a >= 1e3) return `${sign}${(a / 1e3).toFixed(1)}K`;
  return `${sign}${a.toLocaleString('en-IN', { maximumFractionDigits: 2 })}`;
}

/** +1.40% / -0.70%, with an arrow when asked (colour is never the only signal). */
export function formatPct(value: number | string | null | undefined, opts: { decimals?: number; arrow?: boolean; fallback?: string } = {}): string {
  const n = finite(value);
  if (n === null) return opts.fallback ?? '—';
  const text = `${Math.abs(n).toFixed(opts.decimals ?? 2)}%`;
  if (n === 0) return text;
  return `${opts.arrow ? (n > 0 ? '▲ ' : '▼ ') : ''}${n > 0 ? '+' : '-'}${text}`;
}
