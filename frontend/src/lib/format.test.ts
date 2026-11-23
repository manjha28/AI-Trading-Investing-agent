import { describe, expect, it } from 'vitest';
import { formatCompact, formatINR, formatPct } from './format';

describe('formatINR', () => {
  it('uses lakh and crore grouping with the rupee sign', () => {
    expect(formatINR(123456)).toBe('₹1,23,456.00');
    expect(formatINR(12345678.5)).toBe('₹1,23,45,678.50');
    expect(formatINR(-1500)).toBe('-₹1,500.00');
    expect(formatINR(99.5, { decimals: 0 })).toBe('₹100');
  });
  it('shows unknown as a dash, never zero', () => {
    for (const v of [null, undefined, '', 'abc', NaN]) expect(formatINR(v as never)).toBe('—');
    expect(formatINR(0)).toBe('₹0.00');
  });
});
describe('formatCompact', () => {
  it('uses K, L and Cr', () => {
    expect(formatCompact(950)).toBe('950');
    expect(formatCompact(12300)).toBe('12.3K');
    expect(formatCompact(450000)).toBe('4.50L');
    expect(formatCompact(12000000)).toBe('1.20Cr');
    expect(formatCompact(null)).toBe('—');
  });
});
describe('formatPct', () => {
  it('signs the value and adds an arrow on request', () => {
    expect(formatPct(1.4)).toBe('+1.40%');
    expect(formatPct(-0.7, { arrow: true })).toBe('▼ -0.70%');
    expect(formatPct(2, { arrow: true, decimals: 1 })).toBe('▲ +2.0%');
    expect(formatPct(0, { arrow: true })).toBe('0.00%');
    expect(formatPct(undefined)).toBe('—');
  });
});
