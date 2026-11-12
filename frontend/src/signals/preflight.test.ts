import { describe, expect, it } from 'vitest';
import { dataState, preflight, type Quality } from './preflight';

const names: Record<string, string> = { a: 'SBIN', b: 'RELIANCE', c: 'PCJEWELLER' };
const nameOf = (id: string) => names[id];
const q = (over: Partial<Quality>): Quality => ({ status: 'ok', bars: 7000, missing_pct: 0.5, issues: [], ...over });

describe('preflight', () => {
  it('summarises how many instruments are ready and names each exclusion with its reason', () => {
    const p = preflight(['a', 'b', 'c'], { a: q({ missing_pct: 5.3, status: 'refuse' }), b: q({}), c: q({ missing_pct: 0.7 }) }, nameOf, 5);
    expect(p.summary).toBe('2 of 3 instruments ready · SBIN 5.3% missing exceeds the 5.0% limit · the excluded ones will be skipped and reported');
    expect(p.eligible).toBe(2);
    expect(p.canRun).toBe(true);                                     // some instruments are eligible, so Run stays enabled
  });

  it('disables running only when nothing is eligible', () => {
    const none = preflight(['a'], { a: q({ bars: 0, missing_pct: null, status: 'refuse' }) }, nameOf, 5);
    expect(none.canRun).toBe(false);
    expect(none.summary).toMatch(/0 of 1 instrument ready · SBIN no stored 1-minute data/);
    expect(none.summary).toMatch(/nothing can run/);
  });

  it('shows checking while quality is unknown and never treats unknown as ready', () => {
    const p = preflight(['a', 'b'], { a: undefined, b: q({}) }, nameOf, 5);
    expect(p.rows[0].state).toBe('checking');
    expect(p.summary).toMatch(/1 of 2 instruments ready/);
    expect(p.summary).toMatch(/1 still checking/);
    expect(preflight(['a'], {}, nameOf, 5).summary).toBe('Checking data quality…');
    expect(preflight([], {}, nameOf, 5).summary).toBe('Choose at least one instrument.');
  });

  it('classifies stored bar counts', () => {
    expect([dataState(0), dataState(null), dataState(undefined)]).toEqual(['none', 'none', 'none']);
    expect(dataState(3740)).toBe('thin');
    expect(dataState(182347)).toBe('ready');
  });
});
