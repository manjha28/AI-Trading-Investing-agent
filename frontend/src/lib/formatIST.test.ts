import { describe, expect, it } from 'vitest';
import { formatIST, formatISTDate } from './formatIST';

describe('formatIST', () => {
  it('shows India time in one format whatever the input zone', () => {
    expect(formatIST('2026-11-21T07:56:00Z')).toBe('21 Nov 2026, 13:26 IST');
    expect(formatIST('2026-11-21T13:26:00+05:30')).toBe('21 Nov 2026, 13:26 IST');
    expect(formatIST('2026-11-05T18:40:00Z')).toBe('6 Nov 2026, 00:10 IST');                    // crosses midnight in IST
  });
  it('renders missing or invalid values as the fallback, never Invalid Date', () => {
    expect(formatIST(null)).toBe('—');
    expect(formatIST(undefined, 'Never')).toBe('Never');
    expect(formatIST('not a date')).toBe('—');
    expect(formatISTDate('2026-11-21T20:00:00Z')).toBe('22 Nov 2026');
  });
});
