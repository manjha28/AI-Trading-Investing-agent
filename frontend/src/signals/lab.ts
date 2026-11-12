import { apiRequest } from '../api';
import type { Compare, Condition, Definition, IndicatorInfo, Meta, Operand } from './types';

export const DISCLAIMER = 'Decision support, not investment advice. Historical statistics only; no orders are placed.';

export function lab<T>(path: string, init: { method?: string; body?: unknown } = {}): Promise<T> {
  return apiRequest<T>(`/signals-lab${path}`, {
    method: init.method ?? 'GET',
    ...(init.body !== undefined ? { body: JSON.stringify(init.body) } : {}),
  });
}

export function messageOf(err: unknown): string {
  if (err && typeof err === 'object') {
    const e = err as { message?: unknown; detail?: unknown };
    const detail = e.detail as { errors?: { message: string }[]; message?: string } | string | undefined;
    if (detail && typeof detail === 'object') {
      if (Array.isArray(detail.errors) && detail.errors.length) return detail.errors.map((x) => x.message).join('; ');
      if (detail.message) return detail.message;
    }
    if (typeof detail === 'string') return detail;
    if (typeof e.message === 'string') return e.message;
  }
  return 'Request failed';
}

export function emptyDefinition(market: Definition['market'] = 'equity_in'): Definition {
  return {
    schema_version: 'intraday-strategy/1', name: '', description: '', market, universe: [], base_timeframe: '1m', decision_timeframe: '5m', indicators: [],
    long_entry: null, short_entry: null, long_exit: null, short_exit: null,
    sizing: { method: 'percent_equity', value: 10 },
    stop: { mode: 'none', value: 0, atr_length: 14 }, target: { mode: 'none', value: 0, atr_length: 14 }, trail: { mode: 'none', value: 0, atr_length: 14 },
    session: { no_entry_after: null, square_off: null }, constraints: { allow_long: true, allow_short: false, max_trades_per_day: null },
    python: null, source: { type: 'visual', artifact: null, provenance: {} },
  };
}

export function newCompare(ref?: string): Compare {
  return {
    type: 'compare',
    left: ref ? { kind: 'indicator', ref } : { kind: 'price', field: 'close', timeframe: '1m' },
    op: '>', right: { kind: 'const', value: 0 }, within: 1,
  };
}

export function describeOperand(op: Operand, decls: Definition['indicators']): string {
  if (op.kind === 'const') return String(op.value);
  if (op.kind === 'price') return `${op.field}@${op.timeframe}${op.offset ? `[-${op.offset}]` : ''}`;
  if (op.kind === 'indicator') {
    const d = decls.find((x) => x.alias === op.ref);
    const out = op.output && d ? `.${op.output}` : '';
    return d ? `${d.indicator.toUpperCase()}(${Object.values(d.params).join(',')})${out}@${d.timeframe}${op.offset ? `[-${op.offset}]` : ''}` : op.ref;
  }
  return `(${describeOperand(op.left, decls)} ${op.op} ${describeOperand(op.right, decls)})`;
}

export function describeCondition(c: Condition, decls: Definition['indicators']): string {
  if (c.type === 'time_window') return `time ${c.start}–${c.end}`;
  if (c.type === 'group') return c.op === 'not' ? `NOT (${c.children.map((x) => describeCondition(x, decls)).join('')})` : `(${c.children.map((x) => describeCondition(x, decls)).join(` ${c.op.toUpperCase()} `)})`;
  return `${describeOperand(c.left, decls)} ${c.op.replace('_', ' ')} ${describeOperand(c.right, decls)}`;
}

export function nextAlias(base: string, existing: string[]): string {
  const root = base.toLowerCase().replace(/[^a-z0-9_]/g, '_');
  let n = 1;
  let alias = root;
  while (existing.includes(alias)) alias = `${root}_${++n}`;
  return alias;
}

export function defaultParams(info: IndicatorInfo): Record<string, number | string> {
  const out: Record<string, number | string> = {};
  for (const [k, p] of Object.entries(info.params)) if (k !== 'source' || p.default !== 'close') out[k] = p.default;
  return out;
}

export function fmtMoney(v: string | number | null | undefined): string {
  if (v === null || v === undefined || v === '') return '—';
  const n = Number(v);
  return Number.isFinite(n) ? n.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : String(v);
}

export function fmtPct(v: number | string | null | undefined, digits = 2): string {
  if (v === null || v === undefined || v === '') return '—';
  const n = Number(v);
  return Number.isFinite(n) ? `${n.toFixed(digits)}%` : '—';
}

export function fmtTime(iso: string | null | undefined): string {
  if (!iso) return '—';
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString('en-IN', { dateStyle: 'medium', timeStyle: 'short', timeZone: 'Asia/Kolkata' });
}

export function metaIndicator(meta: Meta | null, id: string): IndicatorInfo | undefined {
  return meta?.indicators.find((i) => i.id === id);
}
