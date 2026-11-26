import { describe, expect, it } from 'vitest';
import { emptyDefinition, newCompare, readBack } from './lab';
import { DEFAULT_VALUES, TEMPLATES, detectTemplate, templateRules } from './templates';

const ema = (alias: string, length: number) => ({ alias, indicator: 'ema', params: { length }, timeframe: '5m' });

describe('first-time builder help', () => {
  it('starts a condition that means something, never "price above zero"', () => {
    expect(newCompare([], '15m')).toMatchObject({ left: { field: 'close', timeframe: '15m' }, op: '>', right: { kind: 'price', offset: 1 } });
    expect(newCompare([ema('a', 9)])).toMatchObject({ op: 'crosses_above', right: { ref: 'a' } });
    expect(newCompare([ema('a', 9), ema('b', 21)])).toMatchObject({ left: { ref: 'a' }, right: { ref: 'b' } });
  });

  it('reads the strategy back in ordinary words', () => {
    const d = { ...emptyDefinition(), indicators: [ema('a', 9), ema('b', 21)], universe: ['x'] };
    d.long_entry = newCompare(d.indicators);
    const text = readBack(d).join(' ');
    expect(text).toContain('Buy when EMA(9) crosses above EMA(21).');
    expect(text).toContain('no stop-loss, no target');
    expect(text).toContain('on 1 stock.');
  });
});

describe('guided templates', () => {
  it('each template is recognised again with its numbers, and anything else is "custom"', () => {
    const base = emptyDefinition();
    expect(detectTemplate(base).kind).toBe('empty');
    for (const t of TEMPLATES) {
      const d = { ...base, ...templateRules(t.kind, { ...DEFAULT_VALUES, fast: 5, level: 35, range: 30 }, '15m') };
      expect(detectTemplate(d).kind).toBe(t.kind);
      expect(readBack({ ...d, universe: ['x'] })[0]).toMatch(/^Buy when /);
    }
    expect(detectTemplate({ ...base, ...templateRules('cross', DEFAULT_VALUES, '5m'), short_entry: newCompare([]) }).kind).toBe('custom');
  });
});
