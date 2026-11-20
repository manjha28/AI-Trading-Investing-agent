// Display helpers only. Money arrives from the backend as decimal strings and is never recomputed here.
export const show = (v: unknown): string => (v === null || v === undefined || v === '' ? '—' : String(v));

export function money(v: unknown): string {
  if (v === null || v === undefined || v === '') return '—';
  const s = String(v);
  const n = Number(s);
  if (!Number.isFinite(n)) return s;
  return n.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

export const num = (v: unknown, d = 2): string => {
  if (v === null || v === undefined || v === '' || !Number.isFinite(Number(v))) return '—';
  return Number(v).toFixed(d);
};

export const istTime = (iso: unknown): string => {
  if (!iso) return '—';
  const d = new Date(String(iso));
  return Number.isNaN(d.getTime()) ? '—' : d.toLocaleString('en-IN', { timeZone: 'Asia/Kolkata', hour12: false, year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' });
};

export type Cfg = Record<string, any>;
export type Run = { id: string; status: string; created_at: string; finished_at: string | null; error: string | null; request: Cfg; config_snapshot: Cfg; result: Cfg | null; disclaimer: string };

export const VERIFY_TONE: Record<string, 'ok' | 'info' | 'warn' | 'bad'> = { verified_primary: 'ok', verified_secondary: 'info', owner_stated: 'warn', unverified: 'bad' };
export const VERIFY_LABEL: Record<string, string> = { verified_primary: 'verified (source page)', verified_secondary: 'verified (secondary sources)', owner_stated: 'owner-stated, unverified', unverified: 'UNVERIFIED' };
