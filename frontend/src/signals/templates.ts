import type { Compare, Condition, Definition, Operand } from './types';

/** The ready shapes the guided builder offers. Each is an ordinary strategy definition; the advanced editor can change anything about it. */
export type TemplateKind = 'cross' | 'dip' | 'breakout';
export type TemplateValues = { fast: number; slow: number; direction: 'above' | 'below'; trend: number; rsi: number; level: number; range: number };

export const TEMPLATES: { kind: TemplateKind; label: string; hint: string }[] = [
  { kind: 'cross', label: 'Moving average cross', hint: 'Buy when a fast average overtakes a slow one' },
  { kind: 'dip', label: 'Dip in an uptrend', hint: 'Buy when a pullback ends while the trend is up' },
  { kind: 'breakout', label: 'Range breakout', hint: 'Buy when price leaves its recent range' },
];
export const DEFAULT_VALUES: TemplateValues = { fast: 9, slow: 21, direction: 'above', trend: 50, rsi: 14, level: 40, range: 20 };

const ind = (ref: string, output?: string, offset = 0): Operand => ({ kind: 'indicator', ref, ...(output ? { output } : {}), ...(offset ? { offset } : {}) });
const close = (timeframe: string): Operand => ({ kind: 'price', field: 'close', timeframe });
const compare = (left: Operand, op: Compare['op'], right: Operand): Compare => ({ type: 'compare', left, op, right, within: 1 });

export function templateRules(kind: TemplateKind, v: TemplateValues, tf: string): Pick<Definition, 'indicators' | 'long_entry' | 'decision_timeframe'> {
  const decl = (alias: string, indicator: string, length: number): Definition['indicators'][number] => ({ alias, indicator, params: (indicator === 'donchian' ? { length } : { length, source: 'close' }) as Record<string, number | string>, timeframe: tf });
  if (kind === 'cross') return { decision_timeframe: tf, indicators: [decl('fast', 'ema', v.fast), decl('slow', 'ema', v.slow)], long_entry: compare(ind('fast'), v.direction === 'above' ? 'crosses_above' : 'crosses_below', ind('slow')) };
  if (kind === 'dip') return { decision_timeframe: tf, indicators: [decl('trend', 'ema', v.trend), decl('momentum', 'rsi', v.rsi)],
    long_entry: { type: 'group', op: 'and', children: [compare(close(tf), '>', ind('trend')), compare(ind('momentum'), 'crosses_above', { kind: 'const', value: v.level })] } };
  return { decision_timeframe: tf, indicators: [decl('range', 'donchian', v.range)], long_entry: compare(close(tf), 'crosses_above', ind('range', 'upper', 1)) };
}

const isRef = (o: Operand | undefined, ref: string) => o?.kind === 'indicator' && o.ref === ref;

/** Which template a definition still is. "empty" has no rules yet; "custom" has rules the guided sentences cannot show truthfully. */
export function detectTemplate(d: Definition): { kind: TemplateKind | 'empty' | 'custom'; values: TemplateValues } {
  const values = { ...DEFAULT_VALUES };
  if (!d.long_entry && !d.short_entry && !d.long_exit && !d.short_exit && !d.indicators.length) return { kind: 'empty', values };
  if (d.short_entry || d.long_exit || d.short_exit) return { kind: 'custom', values };
  const length = (alias: string) => Number(d.indicators.find((i) => i.alias === alias)?.params.length);
  const names = d.indicators.map((i) => `${i.alias}:${i.indicator}:${i.timeframe === d.decision_timeframe}`).join();
  const e: Condition | null = d.long_entry;
  if (names === 'fast:ema:true,slow:ema:true' && e?.type === 'compare' && isRef(e.left, 'fast') && isRef(e.right, 'slow') && (e.op === 'crosses_above' || e.op === 'crosses_below')) {
    return { kind: 'cross', values: { ...values, fast: length('fast'), slow: length('slow'), direction: e.op === 'crosses_above' ? 'above' : 'below' } };
  }
  if (names === 'trend:ema:true,momentum:rsi:true' && e?.type === 'group' && e.op === 'and' && e.children.length === 2) {
    const [a, b] = e.children;
    if (a.type === 'compare' && a.op === '>' && isRef(a.right, 'trend') && b.type === 'compare' && isRef(b.left, 'momentum') && b.op === 'crosses_above' && b.right.kind === 'const') {
      return { kind: 'dip', values: { ...values, trend: length('trend'), rsi: length('momentum'), level: b.right.value } };
    }
  }
  if (names === 'range:donchian:true' && e?.type === 'compare' && e.op === 'crosses_above' && isRef(e.right, 'range')) return { kind: 'breakout', values: { ...values, range: length('range') } };
  return { kind: 'custom', values };
}
