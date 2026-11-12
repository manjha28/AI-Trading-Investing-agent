import { describe, expect, it } from 'vitest';
import { formatChange } from './formatChange';

const money = { money: true, currency: 'INR' };

describe('formatChange', () => {
  it('positive to positive keeps ordinary percentage growth', () => {
    expect(formatChange({ year_on_year_growth_pct: '28.64', change_kind: 'growth', absolute_change: '10' }, money)).toEqual({ text: '28.6%', detail: null, tone: 'pos' });
    expect(formatChange({ year_on_year_growth_pct: '-5', change_kind: 'growth', absolute_change: '-5' }, money).tone).toBe('neg');
  });
  it('positive to negative is "Turned negative" with the absolute change, never -413%', () => {
    const view = formatChange({ year_on_year_growth_pct: null, change_kind: 'turned_negative', absolute_change: '-1534200000' }, money);
    expect(view.text).toBe('Turned negative');
    expect(view.detail).toBe('Δ -₹153.4 Cr');
    expect(`${view.text}${view.detail}`).not.toMatch(/%/);
    expect(view.tone).toBe('neg');
  });
  it('negative to positive is "Turned positive" with the absolute change', () => {
    const view = formatChange({ year_on_year_growth_pct: null, change_kind: 'turned_positive', absolute_change: '2500000000' }, money);
    expect(view).toEqual({ text: 'Turned positive', detail: 'Δ +₹250 Cr', tone: 'pos' });
  });
  it('negative to negative shows direction and absolute change, not a percentage', () => {
    expect(formatChange({ year_on_year_growth_pct: null, change_kind: 'both_negative_narrowed', absolute_change: '50000000' }, money))
      .toEqual({ text: 'Less negative', detail: 'Δ +₹5 Cr', tone: 'pos' });
    expect(formatChange({ year_on_year_growth_pct: null, change_kind: 'both_negative_widened', absolute_change: '-50000000' }, money))
      .toEqual({ text: 'More negative', detail: 'Δ -₹5 Cr', tone: 'neg' });
  });
  it('zero and missing previous values are explicit, never a percentage or a zero', () => {
    expect(formatChange({ year_on_year_growth_pct: null, change_kind: 'from_zero', absolute_change: '10000000' }, money).text).toBe('From zero');
    expect(formatChange({ year_on_year_growth_pct: null, change_kind: 'unchanged', absolute_change: '0' }, money).text).toBe('Unchanged');
    expect(formatChange({ year_on_year_growth_pct: null, change_kind: 'unavailable', absolute_change: null }, money)).toEqual({ text: '—', detail: null, tone: 'none' });
    expect(formatChange(undefined, money).text).toBe('—');
  });
  it('per-share metrics are not converted to Crores and non-INR is not assumed INR', () => {
    expect(formatChange({ year_on_year_growth_pct: null, change_kind: 'turned_positive', absolute_change: '5.25' }, { money: false, currency: 'INR' }).detail).toBe('Δ +5.25');
    expect(formatChange({ year_on_year_growth_pct: null, change_kind: 'turned_negative', absolute_change: '-2000000' }, { money: true, currency: 'USD' }).detail).toBe('Δ USD -2,000,000');
  });
  it('older stored reports without change_kind still show their percentage or a dash', () => {
    expect(formatChange({ year_on_year_growth_pct: '12.5' }, money).text).toBe('12.5%');
    expect(formatChange({ year_on_year_growth_pct: null }, money).text).toBe('—');
    // an older stored report containing the misleading -413.4% is re-presented, not repeated
    expect(formatChange({ year_on_year_growth_pct: '-413.4' }, money)).toEqual({ text: 'Turned negative', detail: null, tone: 'neg' });
    expect(formatChange({ year_on_year_growth_pct: '-100' }, money).text).toBe('-100%');
  });
});
