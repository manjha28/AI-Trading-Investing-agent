/** One date formatter for the whole app (except the Trade journal): 21 Nov 2026, 13:26 IST. Always India time, whatever the browser's zone. Missing or invalid values render as the fallback, never as "Invalid Date". */
const FORMAT = new Intl.DateTimeFormat('en-GB', { timeZone: 'Asia/Kolkata', day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' });
const DATE_ONLY = new Intl.DateTimeFormat('en-GB', { timeZone: 'Asia/Kolkata', day: 'numeric', month: 'short', year: 'numeric' });

const toDate = (value: string | number | Date | null | undefined): Date | null => {
  if (value === null || value === undefined || value === '') return null;
  const d = value instanceof Date ? value : new Date(value);
  return Number.isNaN(d.getTime()) ? null : d;
};

export function formatIST(value: string | number | Date | null | undefined, fallback = '—'): string {
  const d = toDate(value);
  if (!d) return fallback;
  return `${FORMAT.format(d).replace(',', ',')} IST`;
}

export function formatISTDate(value: string | number | Date | null | undefined, fallback = '—'): string {
  const d = toDate(value);
  return d ? DATE_ONLY.format(d) : fallback;
}
