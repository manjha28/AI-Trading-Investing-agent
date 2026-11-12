/** Pre-flight data-quality verdict shown BEFORE a backtest runs. Mirrors the server's rule (refuse above the missing-candle limit); the server stays authoritative. */
export type Quality = { status: string; bars: number; missing_pct: number | null; issues: string[] };

export type PreflightRow = { id: string; name: string; state: 'eligible' | 'excluded' | 'checking'; reason: string | null; quality: Quality | null };
export type Preflight = { rows: PreflightRow[]; eligible: number; total: number; excluded: PreflightRow[]; canRun: boolean; summary: string };

export const THIN_BARS = 4_000;       // about ten NSE sessions of 1-minute candles

export function preflight(chosen: string[], quality: Record<string, Quality | undefined>, nameOf: (id: string) => string, maxMissingPct: number): Preflight {
  const rows: PreflightRow[] = chosen.map((id) => {
    const q = quality[id] ?? null;
    if (!q) return { id, name: nameOf(id), state: 'checking', reason: null, quality: null };
    if (!q.bars) return { id, name: nameOf(id), state: 'excluded', reason: 'no stored 1-minute data in this window', quality: q };
    if (q.missing_pct != null && q.missing_pct > maxMissingPct) return { id, name: nameOf(id), state: 'excluded', reason: `${q.missing_pct}% missing exceeds the ${maxMissingPct.toFixed(1)}% limit`, quality: q };
    if (q.status === 'refuse') return { id, name: nameOf(id), state: 'excluded', reason: q.issues[0] ?? 'data quality check refused this window', quality: q };
    return { id, name: nameOf(id), state: 'eligible', reason: null, quality: q };
  });
  const eligible = rows.filter((r) => r.state === 'eligible').length;
  const excluded = rows.filter((r) => r.state === 'excluded');
  const checking = rows.filter((r) => r.state === 'checking').length;
  let summary: string;
  if (!rows.length) summary = 'Choose at least one instrument.';
  else if (checking === rows.length) summary = 'Checking data quality…';
  else {
    summary = `${eligible} of ${rows.length} instrument${rows.length === 1 ? '' : 's'} ready`;
    if (excluded.length) summary += ` · ${excluded.map((r) => `${r.name} ${r.reason}`).join(' · ')}`;
    if (checking) summary += ` · ${checking} still checking`;
    if (excluded.length && eligible) summary += ' · the excluded ones will be skipped and reported';
    if (!eligible && !checking) summary += ' · nothing can run: fetch more data or widen the window';
  }
  return { rows, eligible, total: rows.length, excluded, canRun: eligible > 0, summary };
}

export type DataState = 'ready' | 'thin' | 'none';
export function dataState(bars: number | null | undefined): DataState {
  if (!bars) return 'none';
  return bars < THIN_BARS ? 'thin' : 'ready';
}
