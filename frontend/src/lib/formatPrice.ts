/** NSE equity prices move in 0.05 steps. Every equity price shown in the app goes through this one formatter so Market watch and Research show the same figure for the same price. Display only: stored values are untouched. */
export const NSE_TICK_PAISE = 5;

export function roundToTick(value: number, tickPaise = NSE_TICK_PAISE): number {
  return Math.round(Math.round(value * 100) / tickPaise) * tickPaise / 100;
}

export function tickPrice(value: string | number | null | undefined, fallback = '—'): string {
  if (value === null || value === undefined || value === '') return fallback;
  const n = Number(value);
  if (!Number.isFinite(n)) return fallback;
  return roundToTick(n).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}
