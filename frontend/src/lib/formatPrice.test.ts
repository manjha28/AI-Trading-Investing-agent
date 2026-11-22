import { describe, expect, it } from 'vitest';
import { roundToTick, tickPrice } from './formatPrice';

describe('tickPrice', () => {
  it('rounds to the NSE tick of 0.05 and always shows two decimals', () => {
    expect(tickPrice('223.46')).toBe('223.45');
    expect(tickPrice('101.53')).toBe('101.55');
    expect(tickPrice(100)).toBe('100.00');
    expect(tickPrice('1234567.89')).toBe('12,34,567.90');
    expect(roundToTick(0.07)).toBe(0.05);
  });
  it('shows missing or invalid prices as the fallback, never 0.00', () => {
    expect(tickPrice(null)).toBe('—');
    expect(tickPrice('')).toBe('—');
    expect(tickPrice('abc')).toBe('—');
  });
});
