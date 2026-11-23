/** One place that decides a card's entry state, band text and R label, so a card and its list row can never disagree.
 *  Rules: in band (price inside the entry zone), above band, below band, or first target already below price (a stale setup). Never a bare "Wait for entry". */
export type Tone = 'positive' | 'negative' | 'caution' | 'info' | 'neutral';
export type CardLevels = { status?: string; reason?: string; entry_zone?: { low: string; high: string }; reference_close?: string; entry_label?: string; entry_status?: string; stop?: string; targets?: { label: string; price: string; risk_reward?: number }[] };
export type CardState = { kind: 'in_band' | 'above_band' | 'below_band' | 'target_below_close' | 'no_price' | 'no_levels'; short: string; label: string; tone: Tone; reason: string | null };

const money = (n: number): string => `₹${n.toFixed(2)}`;
const num = (v: string | number | null | undefined): number | null => { if (v === null || v === undefined || v === '') return null; const n = Number(v); return Number.isFinite(n) ? n : null; };

export function cardState(levels: CardLevels | null | undefined, lastClose?: string | number | null): CardState {
  if (!levels || levels.status !== 'available') return { kind: 'no_levels', short: 'No levels', label: `No levels: ${levels?.reason ?? 'none for this horizon'}`, tone: 'neutral', reason: levels?.reason ?? null };
  const lo = num(levels.entry_zone?.low); const hi = num(levels.entry_zone?.high);
  const close = num(lastClose) ?? num(levels.reference_close);
  const t1 = num(levels.targets?.[0]?.price);
  if (close === null || lo === null || hi === null) return { kind: 'no_price', short: 'Wait: no price', label: 'Wait: no current price or entry band to compare', tone: 'caution', reason: 'missing price or band' };
  if (t1 !== null && t1 <= close) {
    const reason = `first target ${money(t1)} is already at or below the price ${money(close)}`;
    return { kind: 'target_below_close', short: 'Wait: target passed', label: `Wait: ${reason}`, tone: 'caution', reason };
  }
  if (close >= lo && close <= hi) return { kind: 'in_band', short: 'In band', label: levels.entry_label ?? 'In buy band', tone: 'positive', reason: null };
  const band = `${money(lo)}–${money(hi)}`;
  if (close > hi) { const reason = `price ${money(close)} is above the band ${band}; needs a pullback`; return { kind: 'above_band', short: 'Wait: above band', label: levels.entry_label ?? `Wait: ${reason}`, tone: 'caution', reason }; }
  const reason = `price ${money(close)} is below the band ${band}; needs to recover`;
  return { kind: 'below_band', short: 'Wait: below band', label: levels.entry_label ?? `Wait: ${reason}`, tone: 'caution', reason };
}

/** The entry band as text, or an em dash when there is none. */
export function bandText(levels: CardLevels | null | undefined): string {
  const lo = num(levels?.entry_zone?.low); const hi = num(levels?.entry_zone?.high);
  return lo === null || hi === null ? '—' : `${money(lo)}–${money(hi)}`;
}

/** "T1 2R": the first target and its reward-to-risk, with nothing invented when the engine gave no R. */
export function rLabel(levels: CardLevels | null | undefined): string {
  const t = levels?.targets?.[0];
  if (!t) return '—';
  return t.risk_reward ? `${t.label.replace(/\s*R$/i, '')}${/R$/i.test(t.label) ? 'R' : ` · ${t.risk_reward}R`}` : t.label;
}
