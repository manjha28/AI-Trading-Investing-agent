import { useState } from 'react';
import { DISCLAIMER, readBack } from './lab';
import { DEFAULT_VALUES, TEMPLATES, detectTemplate, templateRules, type TemplateKind, type TemplateValues } from './templates';
import type { Definition, InstrumentRow, Meta } from './types';

type Props = {
  d: Definition; patch: (changes: Partial<Definition>) => void; meta: Meta; instruments: InstrumentRow[];
  query: string; setQuery: (q: string) => void; toggle: (id: string) => void; nameOf: (id: string) => string; addPreset: (key: 'nifty50' | 'watchlist') => void; error: string | null;
  todo: string[]; warnings: string[]; canSave: boolean; saving: boolean; save: (andTest: boolean) => void;
};

function Num({ label, value, onChange, min = 1, step = 1 }: { label: string; value: number; onChange: (n: number) => void; min?: number; step?: number }) {
  return <input className="gb-blank" type="number" aria-label={label} min={min} step={step} value={Number.isFinite(value) ? value : ''} onChange={(e) => onChange(Number(e.target.value))} />;
}

const Step = ({ n, title, children }: { n: number; title: string; children: React.ReactNode }) => (
  <section className="gb-step" aria-label={title}><span className="gb-num" aria-hidden="true">{n}</span><div><h3>{title}</h3>{children}</div></section>
);

/** Five plain steps over the same strategy definition the advanced editor edits: start, when to buy, when to get out, which stocks, check and save. */
export function GuidedBuilder({ d, patch, meta, instruments, query, setQuery, toggle, nameOf, addPreset, error, todo, warnings, canSave, saving, save }: Props) {
  const [picking, setPicking] = useState(false);
  const found = detectTemplate(d);
  const v = found.values;
  const tf = d.decision_timeframe;
  const apply = (kind: TemplateKind, values: TemplateValues, timeframe = tf) => patch(templateRules(kind, values, timeframe));
  const start = (kind: TemplateKind) => patch({
    ...templateRules(kind, DEFAULT_VALUES, tf), short_entry: null, long_exit: null, short_exit: null,
    ...(d.stop.mode === 'none' ? { stop: { ...d.stop, mode: 'percent', value: 1 }, target: { ...d.target, mode: 'rr', value: 2 } } : {}),
  });
  const set = (changes: Partial<TemplateValues>) => { if (found.kind !== 'empty' && found.kind !== 'custom') apply(found.kind, { ...v, ...changes }); };
  const stopPct = d.stop.mode === 'percent' ? d.stop.value : 0;
  const reward = d.target.mode === 'rr' ? d.target.value : 0;
  const checks = [
    { ok: !!d.long_entry || !!d.short_entry, label: 'A rule for when to buy' }, { ok: d.stop.mode !== 'none', label: 'A stop-loss' },
    { ok: d.universe.length > 0, label: 'At least one stock' }, { ok: d.name.trim().length > 0, label: 'A name' },
  ];

  return (
    <div className="gb">
      <div className="gb-steps">
        <Step n={1} title="Start from">
          <div className="gb-choices" role="group" aria-label="Starting point">
            {TEMPLATES.map((t) => <button key={t.kind} type="button" className={found.kind === t.kind ? 'on' : ''} aria-pressed={found.kind === t.kind} onClick={() => start(t.kind)}><strong>{t.label}</strong><small>{t.hint}</small></button>)}
          </div>
        </Step>
        <Step n={2} title="When to buy">
          {found.kind === 'empty' && <p className="sl-muted">Pick a starting point above. Every number in it can be changed.</p>}
          {found.kind === 'custom' && <><p className="gb-say">{readBack(d)[0] ?? 'This strategy has its own conditions.'}</p><p className="sl-muted">Change these conditions in the advanced editor.</p></>}
          {found.kind === 'cross' && <p className="gb-say">Buy when the <Num label="Fast average length" value={v.fast} onChange={(fast) => set({ fast })} /> candle average crosses{' '}
            <select className="gb-blank" aria-label="Cross direction" value={v.direction} onChange={(e) => set({ direction: e.target.value as 'above' | 'below' })}><option value="above">above</option><option value="below">below</option></select>{' '}
            the <Num label="Slow average length" value={v.slow} onChange={(slow) => set({ slow })} /> candle average.</p>}
          {found.kind === 'dip' && <p className="gb-say">Buy when price is above its <Num label="Trend average length" value={v.trend} onChange={(trend) => set({ trend })} /> candle average and RSI{' '}
            (<Num label="RSI length" value={v.rsi} onChange={(rsi) => set({ rsi })} />) climbs back above <Num label="RSI level" value={v.level} onChange={(level) => set({ level })} />.</p>}
          {found.kind === 'breakout' && <p className="gb-say">Buy when price closes above the highest high of the previous <Num label="Range length" value={v.range} onChange={(range) => set({ range })} /> candles.</p>}
          {found.kind !== 'empty' && <label className="gb-inline">Candle size{' '}
            <select className="gb-blank" aria-label="Candle size" value={tf} onChange={(e) => (found.kind === 'custom' ? undefined : apply(found.kind as TemplateKind, v, e.target.value))} disabled={found.kind === 'custom'}>{meta.timeframes.map((t) => <option key={t}>{t}</option>)}</select></label>}
        </Step>
        <Step n={3} title="When to get out">
          <p className="gb-say">Stop the loss <Num label="Stop-loss percent" min={0} step={0.1} value={stopPct} onChange={(value) => patch({ stop: { ...d.stop, mode: value > 0 ? 'percent' : 'none', value: value > 0 ? value : 0 }, ...(value > 0 ? {} : { target: { ...d.target, mode: 'none', value: 0 } }) })} />% below the buy price.
            Take profit at <Num label="Target in times the risk" min={0} step={0.5} value={reward} onChange={(value) => patch({ target: { ...d.target, mode: value > 0 && d.stop.mode !== 'none' ? 'rr' : 'none', value: value > 0 ? value : 0 } })} /> times that risk.
            Anything still open closes at <input className="gb-blank" type="time" aria-label="Square-off time" value={d.session.square_off ?? '15:15'} onChange={(e) => patch({ session: { ...d.session, square_off: e.target.value || null } })} />.</p>
          <p className="sl-muted">0 means none. These levels are used in tests only; nothing here places an order.</p>
        </Step>
        <Step n={4} title="Which stocks">
          <div className="gb-row"><button type="button" onClick={() => addPreset('nifty50')}>Add Nifty 50</button><button type="button" onClick={() => addPreset('watchlist')}>Add my watchlist</button>
            <button type="button" aria-expanded={picking} onClick={() => setPicking(!picking)}>Pick one by one</button>{d.universe.length > 0 && <button type="button" onClick={() => patch({ universe: [] })}>Clear</button>}</div>
          <p className="sl-muted">{d.universe.length} stock{d.universe.length === 1 ? '' : 's'} chosen.</p>
          {error && <p className="sl-todo" role="alert">{error}</p>}
          {d.universe.length > 0 && <div className="sl-chips" aria-label="Selected instruments">{d.universe.slice(0, 20).map((id) => <button type="button" key={id} className="sl-chip" title="Remove" onClick={() => toggle(id)}>{nameOf(id)} ✕</button>)}{d.universe.length > 20 && <span className="sl-muted">and {d.universe.length - 20} more</span>}</div>}
          {picking && <div className="gb-pick"><input aria-label="Search stocks" placeholder="Symbol or name" value={query} onChange={(e) => setQuery(e.target.value)} />
            <ul>{instruments.slice(0, 30).map((i) => <li key={i.id}><span>{i.symbol}</span><button type="button" aria-label={`${d.universe.includes(i.id) ? 'Remove' : 'Add'} ${i.symbol}`} onClick={() => toggle(i.id)}>{d.universe.includes(i.id) ? 'Remove' : 'Add'}</button></li>)}
              {!instruments.length && <li className="sl-muted">No stock with stored 1-minute prices{query ? ' matches' : ''}.</li>}</ul></div>}
        </Step>
        <Step n={5} title="Save and test">
          <div className="gb-row"><button type="button" className="sl-primary" disabled={!canSave} onClick={() => save(true)}>{saving ? 'Saving…' : 'Save and test on past prices'}</button><button type="button" disabled={!canSave} onClick={() => save(false)}>Save without testing</button></div>
          {!canSave && !saving && todo.length > 0 && <p className="sl-todo" role="status">To save: {todo.join('; ')}.</p>}
          {warnings.map((w) => <p className="sl-todo" key={w}>{w}</p>)}
        </Step>
      </div>
      <aside className="gb-side" aria-label="Your strategy">
        <label>Name<input aria-label="Strategy name" value={d.name} maxLength={120} placeholder="e.g. Morning average cross" onChange={(e) => patch({ name: e.target.value })} /></label>
        <h3>Your strategy in words</h3>
        <div className="sl-readback">{readBack(d).map((s) => <p key={s}>{s}</p>)}{!d.long_entry && !d.short_entry && <p className="sl-muted">It will appear here as you choose.</p>}</div>
        <h3>Before you save</h3>
        <ul className="gb-checks">{checks.map((c) => <li key={c.label} className={c.ok ? 'ok' : ''}><span aria-hidden="true">{c.ok ? '✓' : '○'}</span> {c.label}<span className="gb-sr">{c.ok ? ' done' : ' missing'}</span></li>)}</ul>
        <p className="sl-disclaimer">{DISCLAIMER}</p>
      </aside>
    </div>
  );
}
