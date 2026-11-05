import { describe, expect, it } from 'vitest';
import { formatMoney } from './formatMoney';

describe('formatMoney', () => {
  it('uses compact Indian units for INR', () => {
    expect(formatMoney('2400000000', 'INR')).toBe('₹240 Cr');
    expect(formatMoney('300000000000', 'INR')).toBe('₹30,000 Cr');
    expect(formatMoney('1500000000000', 'INR')).toBe('₹1.5 Lakh Cr');
    expect(formatMoney('15000000000000', 'INR')).toBe('₹15 Lakh Cr');
    expect(formatMoney('8500000', 'INR')).toBe('₹85 L');
    expect(formatMoney('12500', 'INR')).toBe('₹12.5 K');
    expect(formatMoney('950', 'INR')).toBe('₹950');
    expect(formatMoney('-2400000000', 'INR')).toBe('-₹240 Cr');
  });
  it('never assumes INR when currency is other or unknown', () => {
    expect(formatMoney('2400000000', 'USD')).toBe('USD 2,400,000,000');
    expect(formatMoney('2400000000', undefined)).toBe('24,00,000,000'.replace('24,00,000,000', (2400000000).toLocaleString('en-IN')));
    expect(formatMoney('2400000000', undefined)).not.toContain('₹');
  });
  it('handles missing values', () => {
    expect(formatMoney(null, 'INR')).toBe('—');
    expect(formatMoney('abc', 'INR')).toBe('—');
  });
});
