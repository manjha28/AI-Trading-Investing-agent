import { describe, expect, it } from 'vitest';
import { viewFromHash } from './viewRoute';

describe('page addresses', () => {
  it('reads a page from the address and ignores anything else', () => {
    expect(viewFromHash('#/journal')).toBe('journal');
    expect(viewFromHash('#/market-data')).toBe('market-watch');
    expect(viewFromHash('#forward-record')).toBeNull();
    expect(viewFromHash('#/nonsense')).toBeNull();
    expect(viewFromHash('')).toBeNull();
  });
});
