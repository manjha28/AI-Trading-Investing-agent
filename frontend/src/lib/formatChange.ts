import { formatMoney } from './formatMoney';

export type ChangeKind =
  | 'growth' | 'turned_negative' | 'turned_positive' | 'turned_zero' | 'from_zero' | 'unchanged' | 'unavailable'
  | 'both_negative_narrowed' | 'both_negative_widened' | 'both_negative_unchanged';

export type ChangeInput = { year_on_year_growth_pct: string | null; change_kind?: ChangeKind; absolute_change?: string | null } | null | undefined;
export type ChangeView = { text: string; detail: string | null; tone: 'pos' | 'neg' | 'none' };

const LABELS: Partial<Record<ChangeKind, [string, ChangeView['tone']]>> = {
  turned_negative: ['Turned negative', 'neg'],
  turned_positive: ['Turned positive', 'pos'],
  turned_zero: ['Reached zero', 'pos'],
  from_zero: ['From zero', 'none'],
  both_negative_narrowed: ['Less negative', 'pos'],
  both_negative_widened: ['More negative', 'neg'],
  both_negative_unchanged: ['Unchanged (negative)', 'none'],
  unchanged: ['Unchanged', 'none'],
};

/**
 * Presentation of a period-over-period change. A percentage is shown only when the backend supplied one
 * (positive base, unambiguous). Across a sign change or from a non-positive base the label and the absolute
 * change are shown instead, so 37.12 -> -116.30 is never rendered as "-413%". Presentation only: raw values
 * are untouched. `money` is false for per-share and similar metrics, which are not converted to Crores.
 */
export function formatChange(item: ChangeInput, opts: { money: boolean; currency: string }): ChangeView {
  if (!item) return { text: '—', detail: null, tone: 'none' };
  // Reports stored before change_kind existed: with a positive base, a fall of more than 100% can only mean the
  // value crossed below zero, so present it as such rather than as a misleading percentage (no absolute change known).
  if (item.change_kind === undefined && item.year_on_year_growth_pct !== null && Number(item.year_on_year_growth_pct) < -100) {
    return { text: 'Turned negative', detail: null, tone: 'neg' };
  }
  const kind = item.change_kind ?? (item.year_on_year_growth_pct !== null ? 'growth' : 'unavailable');
  if (kind === 'growth') {
    const pct = Number(item.year_on_year_growth_pct);
    if (!Number.isFinite(pct)) return { text: '—', detail: null, tone: 'none' };
    return { text: `${pct.toLocaleString('en-IN', { maximumFractionDigits: 1 })}%`, detail: null, tone: pct >= 0 ? 'pos' : 'neg' };
  }
  const label = LABELS[kind];
  if (!label) return { text: '—', detail: null, tone: 'none' };
  let detail: string | null = null;
  const absolute = item.absolute_change == null ? NaN : Number(item.absolute_change);
  if (Number.isFinite(absolute)) {
    const amount = opts.money
      ? formatMoney(item.absolute_change, opts.currency)
      : absolute.toLocaleString('en-IN', { maximumFractionDigits: 2 });
    detail = `Δ ${absolute > 0 ? '+' : ''}${amount}`;
  }
  return { text: label[0], detail, tone: label[1] };
}
