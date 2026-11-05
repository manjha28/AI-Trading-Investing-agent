/** Presentation-only compact money formatting. Never alters stored values; callers pass the raw value. */
const SYMBOLS: Record<string, string> = { INR: '₹' };
const LAKH = 1e5;
const CRORE = 1e7;
const LAKH_CRORE = 1e12;

function trim(value: number): string {
  const abs = Math.abs(value);
  const digits = abs >= 1000 ? 0 : abs >= 100 ? 1 : 2;
  return value.toLocaleString('en-IN', { maximumFractionDigits: digits });
}

/**
 * Compact Indian units (K, L, Cr, Lakh Cr) are used only when the source metadata says the currency is INR.
 * Any other or missing currency is shown as a plain grouped number with the code, never assumed to be INR.
 */
export function formatMoney(raw: string | number | null | undefined, currency: string | null | undefined): string {
  if (raw === null || raw === undefined || raw === '') return '—';
  const value = Number(raw);
  if (!Number.isFinite(value)) return '—';
  const code = (currency ?? '').trim().toUpperCase();
  if (code !== 'INR') {
    const plain = value.toLocaleString(code ? 'en-US' : 'en-IN', { maximumFractionDigits: 2 });
    return code ? `${code} ${plain}` : plain;
  }
  const sign = value < 0 ? '-' : '';
  const abs = Math.abs(value);
  const symbol = SYMBOLS.INR;
  if (abs >= LAKH_CRORE) return `${sign}${symbol}${trim(abs / LAKH_CRORE)} Lakh Cr`;
  if (abs >= CRORE) return `${sign}${symbol}${trim(abs / CRORE)} Cr`;
  if (abs >= LAKH) return `${sign}${symbol}${trim(abs / LAKH)} L`;
  if (abs >= 1000) return `${sign}${symbol}${trim(abs / 1000)} K`;
  return `${sign}${symbol}${abs.toLocaleString('en-IN', { maximumFractionDigits: 2 })}`;
}
