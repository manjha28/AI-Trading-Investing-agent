import type { Compare, Condition, Group, IndicatorDecl, Meta, Operand, TimeWindow } from './types';
import { newCompare } from './lab';

type Ctx = { decls: IndicatorDecl[]; meta: Meta; disabled?: boolean; timeframe?: string };

function OperandEditor({ value, onChange, ctx, side }: { value: Operand; onChange: (o: Operand) => void; ctx: Ctx; side: string }) {
  const first = ctx.decls[0]?.alias;
  const outputs = (ref: string): string[] => {
    const d = ctx.decls.find((x) => x.alias === ref);
    return ctx.meta.indicators.find((i) => i.id === d?.indicator)?.outputs ?? ['value'];
  };
  const switchKind = (kind: Operand['kind']) => {
    if (kind === 'indicator') onChange({ kind, ref: first ?? '', output: first ? outputs(first)[0] : 'value', offset: 0 });
    else if (kind === 'price') onChange({ kind, field: 'close', timeframe: '1m', offset: 0 });
    else if (kind === 'const') onChange({ kind, value: 0 });
    else onChange({ kind: 'arith', op: '*', left: { kind: 'price', field: 'close', timeframe: '1m' }, right: { kind: 'const', value: 1 } });
  };
  return (
    <span className="sl-operand">
      <select aria-label={`${side} type`} value={value.kind} disabled={ctx.disabled} onChange={(e) => switchKind(e.target.value as Operand['kind'])}>
        <option value="indicator">Indicator</option><option value="price">Price / volume</option><option value="const">Number</option><option value="arith">Expression</option>
      </select>
      {value.kind === 'indicator' && (
        <>
          <select aria-label={`${side} indicator`} value={value.ref} disabled={ctx.disabled} onChange={(e) => onChange({ ...value, ref: e.target.value, output: outputs(e.target.value)[0] })}>
            {!ctx.decls.length && <option value="">Add an indicator first</option>}
            {ctx.decls.map((d) => <option key={d.alias} value={d.alias}>{d.alias} · {d.indicator.toUpperCase()}@{d.timeframe}</option>)}
          </select>
          {outputs(value.ref).length > 1 && (
            <select aria-label={`${side} output`} value={value.output ?? ''} disabled={ctx.disabled} onChange={(e) => onChange({ ...value, output: e.target.value })}>
              {outputs(value.ref).map((o) => <option key={o} value={o}>{o}</option>)}
            </select>
          )}
          <input aria-label={`${side} bars back`} className="sl-tiny" type="number" min={0} max={50} value={value.offset ?? 0} disabled={ctx.disabled} title="Bars back (0 = latest completed)" onChange={(e) => onChange({ ...value, offset: Number(e.target.value) })} />
        </>
      )}
      {value.kind === 'price' && (
        <>
          <select aria-label={`${side} field`} value={value.field} disabled={ctx.disabled} onChange={(e) => onChange({ ...value, field: e.target.value })}>
            {ctx.meta.price_fields.map((f) => <option key={f} value={f}>{f}</option>)}
          </select>
          <select aria-label={`${side} timeframe`} value={value.timeframe} disabled={ctx.disabled} onChange={(e) => onChange({ ...value, timeframe: e.target.value })}>
            {ctx.meta.timeframes.map((t) => <option key={t} value={t}>{t}</option>)}
          </select>
          <input aria-label={`${side} bars back`} className="sl-tiny" type="number" min={0} max={50} value={value.offset ?? 0} disabled={ctx.disabled} title="Bars back (0 = latest completed)" onChange={(e) => onChange({ ...value, offset: Number(e.target.value) })} />
        </>
      )}
      {value.kind === 'const' && (
        <input aria-label={`${side} number`} className="sl-num" type="number" step="any" value={Number.isFinite(value.value) ? value.value : 0} disabled={ctx.disabled} onChange={(e) => onChange({ ...value, value: Number(e.target.value) })} />
      )}
      {value.kind === 'arith' && (
        <span className="sl-arith">
          <OperandEditor value={value.left} onChange={(left) => onChange({ ...value, left })} ctx={ctx} side={`${side} left`} />
          <select aria-label={`${side} operator`} value={value.op} disabled={ctx.disabled} onChange={(e) => onChange({ ...value, op: e.target.value as '+' | '-' | '*' | '/' })}>
            {['+', '-', '*', '/'].map((o) => <option key={o} value={o}>{o}</option>)}
          </select>
          <OperandEditor value={value.right} onChange={(right) => onChange({ ...value, right })} ctx={ctx} side={`${side} right`} />
        </span>
      )}
    </span>
  );
}

function CompareRow({ value, onChange, onRemove, ctx }: { value: Compare; onChange: (c: Compare) => void; onRemove: () => void; ctx: Ctx }) {
  return (
    <div className="sl-cond sl-compare">
      <OperandEditor value={value.left} onChange={(left) => onChange({ ...value, left })} ctx={ctx} side="Left" />
      <select aria-label="Comparison" value={value.op} disabled={ctx.disabled} onChange={(e) => onChange({ ...value, op: e.target.value as Compare['op'] })}>
        {ctx.meta.compare_ops.map((o) => <option key={o} value={o}>{o.replace('_', ' ')}</option>)}
      </select>
      <OperandEditor value={value.right} onChange={(right) => onChange({ ...value, right })} ctx={ctx} side="Right" />
      <label className="sl-within" title="True if the event happened within the last N decision candles">within <input aria-label="Within bars" className="sl-tiny" type="number" min={1} max={100} value={value.within ?? 1} disabled={ctx.disabled} onChange={(e) => onChange({ ...value, within: Math.max(1, Number(e.target.value)) })} /></label>
      <button type="button" className="sl-icon" aria-label="Remove condition" disabled={ctx.disabled} onClick={onRemove}>✕</button>
    </div>
  );
}

function TimeRow({ value, onChange, onRemove, ctx }: { value: TimeWindow; onChange: (c: TimeWindow) => void; onRemove: () => void; ctx: Ctx }) {
  return (
    <div className="sl-cond sl-time">
      <span>Session window (market-local)</span>
      <input aria-label="Window start" type="time" value={value.start} disabled={ctx.disabled} onChange={(e) => onChange({ ...value, start: e.target.value })} />
      <span>to</span>
      <input aria-label="Window end" type="time" value={value.end} disabled={ctx.disabled} onChange={(e) => onChange({ ...value, end: e.target.value })} />
      <button type="button" className="sl-icon" aria-label="Remove time window" disabled={ctx.disabled} onClick={onRemove}>✕</button>
    </div>
  );
}

function GroupBox({ value, onChange, onRemove, ctx, depth }: { value: Group; onChange: (g: Group) => void; onRemove: () => void; ctx: Ctx; depth: number }) {
  const setChild = (i: number, c: Condition | null) => {
    const children = value.children.slice();
    if (c === null) children.splice(i, 1); else children[i] = c;
    onChange({ ...value, children });
  };
  return (
    <div className={`sl-group sl-depth-${Math.min(depth, 4)}`} role="group" aria-label={`${value.op.toUpperCase()} group`}>
      <div className="sl-group-head">
        <select aria-label="Group logic" value={value.op} disabled={ctx.disabled} onChange={(e) => onChange({ ...value, op: e.target.value as Group['op'] })}>
          <option value="and">ALL of (AND)</option><option value="or">ANY of (OR)</option><option value="not">NOT</option>
        </select>
        <span className="sl-group-actions">
          <button type="button" disabled={ctx.disabled} onClick={() => onChange({ ...value, children: [...value.children, newCompare(ctx.decls, ctx.timeframe)] })}>+ Condition</button>
          <button type="button" disabled={ctx.disabled || depth >= 5} onClick={() => onChange({ ...value, children: [...value.children, { type: 'group', op: 'or', children: [newCompare(ctx.decls, ctx.timeframe)] }] })}>+ Group</button>
          <button type="button" disabled={ctx.disabled} onClick={() => onChange({ ...value, children: [...value.children, { type: 'time_window', start: '09:30', end: '14:30' }] })}>+ Time window</button>
          <button type="button" className="sl-icon" aria-label="Remove group" disabled={ctx.disabled} onClick={onRemove}>✕</button>
        </span>
      </div>
      <div className="sl-group-body">
        {value.children.map((c, i) => <Node key={i} value={c} onChange={(n) => setChild(i, n)} ctx={ctx} depth={depth + 1} />)}
      </div>
    </div>
  );
}

function Node({ value, onChange, ctx, depth }: { value: Condition; onChange: (c: Condition | null) => void; ctx: Ctx; depth: number }) {
  if (value.type === 'group') return <GroupBox value={value} onChange={onChange} onRemove={() => onChange(null)} ctx={ctx} depth={depth} />;
  if (value.type === 'time_window') return <TimeRow value={value} onChange={onChange} onRemove={() => onChange(null)} ctx={ctx} />;
  return <CompareRow value={value} onChange={onChange} onRemove={() => onChange(null)} ctx={ctx} />;
}

export function ConditionEditor({ title, value, onChange, decls, meta, disabled, timeframe }: {
  title: string; value: Condition | null; onChange: (c: Condition | null) => void; decls: IndicatorDecl[]; meta: Meta; disabled?: boolean; timeframe?: string;
}) {
  const ctx: Ctx = { decls, meta, disabled, timeframe };
  return (
    <section className="sl-rule" aria-label={title}>
      <header><h4>{title}</h4>
        {!value && !disabled && (
          <span className="sl-group-actions">
            <button type="button" onClick={() => onChange(newCompare(decls, timeframe))}>+ Condition</button>
            <button type="button" onClick={() => onChange({ type: 'group', op: 'and', children: [newCompare(decls, timeframe)] })}>+ AND / OR group</button>
          </span>
        )}
      </header>
      {value ? <Node value={value} onChange={onChange} ctx={ctx} depth={1} /> : <p className="sl-muted">No condition. {title.includes('entry') ? 'Add one to enable this side.' : 'Optional: the simulation can also exit on stop, target, trail or square-off.'}</p>}
    </section>
  );
}
