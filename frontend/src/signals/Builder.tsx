import { useEffect, useMemo, useRef, useState } from 'react';
import { ConditionEditor } from './ConditionEditor';
import { DISCLAIMER, defaultParams, describeCondition, lab, messageOf, nextAlias, readBack } from './lab';
import { OptionsPanel } from './OptionsPanel';
import { Field, Notice, Pill, Scroll } from './shared';
import { RULES, type Condition, type Definition, type Operand, type InstrumentRow, type Meta, type RuleName, type StrategyRow, type ValidateResult } from './types';

export type Draft = {
  definition: Definition;
  sourceText: string | null;
  editing: { id: string; name: string; version: number } | null;
};

function retimeOperand(o: Operand, from: string, to: string): Operand {
  if (o.kind === 'price') return o.timeframe === from ? { ...o, timeframe: to } : o;
  if (o.kind === 'arith') return { ...o, left: retimeOperand(o.left, from, to), right: retimeOperand(o.right, from, to) };
  return o;
}
function retimeCondition(c: Condition | null, from: string, to: string): Condition | null {
  if (!c) return c;
  if (c.type === 'group') return { ...c, children: c.children.map((x) => retimeCondition(x, from, to) as Condition) };
  if (c.type === 'compare') return { ...c, left: retimeOperand(c.left, from, to), right: retimeOperand(c.right, from, to) };
  return c;
}

export function Builder({ meta, draft, setDraft, onSaved, onReset }: {
  meta: Meta; draft: Draft; setDraft: (d: Draft) => void; onSaved: (s: StrategyRow, message: string) => void; onReset: () => void;
}) {
  const d = draft.definition;
  const isPython = d.python !== null;
  const [section, setSection] = useState<'logic' | 'indicators' | 'risk' | 'review' | 'options'>(draft.definition.options ? 'options' : 'logic');
  const isOptions = !!d.options;
  const [validation, setValidation] = useState<ValidateResult | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [query, setQuery] = useState('');
  const [category, setCategory] = useState('all');
  const [pick, setPick] = useState('');
  const [instQuery, setInstQuery] = useState('');
  const [instruments, setInstruments] = useState<InstrumentRow[]>([]);
  const [instError, setInstError] = useState<string | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const patch = (changes: Partial<Definition>) => {
    const edited = draft.definition.source.type === 'pine' ? { source: { ...draft.definition.source, provenance: { ...draft.definition.source.provenance, edited_after_import: true } } } : {};
    setDraft({ ...draft, definition: { ...draft.definition, ...changes, ...edited } });
  };

  useEffect(() => {
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => {
      lab<ValidateResult>('/validate', { method: 'POST', body: { definition: d } }).then(setValidation).catch((e) => setValidation({ valid: false, issues: [{ level: 'error', message: messageOf(e) }], requirements: null }));
    }, 350);
    return () => { if (timer.current) clearTimeout(timer.current); };
  }, [d]);

  useEffect(() => {
    let live = true;
    const t = setTimeout(() => {
      lab<InstrumentRow[]>(`/instruments?market=${d.market}&query=${encodeURIComponent(instQuery)}`)
        .then((rows) => { if (live) { setInstruments(rows); setInstError(null); } })
        .catch((e) => { if (live) setInstError(messageOf(e)); });
    }, 250);
    return () => { live = false; clearTimeout(t); };
  }, [d.market, instQuery]);

  const available = useMemo(() => {
    const q = query.trim().toLowerCase();
    return meta.indicators.filter((i) => (category === 'all' || i.category === category) && (!q || i.name.toLowerCase().includes(q) || i.id.includes(q)));
  }, [meta, query, category]);
  const categories = useMemo(() => ['all', ...Array.from(new Set(meta.indicators.map((i) => i.category)))], [meta]);

  const addIndicator = () => {
    const info = meta.indicators.find((i) => i.id === pick) ?? available[0];
    if (!info) return;
    const alias = nextAlias(info.id, d.indicators.map((x) => x.alias));
    const params = defaultParams(info);
    // a second copy of the same indicator starts with a different length, so the two can be told apart
    const used = d.indicators.filter((x) => x.indicator === info.id).map((x) => Number(x.params.length));
    if (used.length && typeof params.length === 'number') { let length = params.length; while (used.includes(length)) length *= 2; params.length = length; }
    patch({ indicators: [...d.indicators, { alias, indicator: info.id, params, timeframe: d.decision_timeframe }] });
  };
  const setIndicator = (i: number, next: Definition['indicators'][number] | null) => {
    const list = d.indicators.slice();
    if (next === null) list.splice(i, 1); else list[i] = next;
    patch({ indicators: list });
  };

  const example = () => {
    const ema = meta.indicators.find((i) => i.id === 'ema');
    if (!ema) return;
    const tf = d.decision_timeframe;
    patch({
      name: d.name.trim() || 'Example: EMA 9 / 21 cross', description: 'Buy when the fast average crosses above the slow one.',
      indicators: [{ alias: 'fast', indicator: 'ema', params: { ...defaultParams(ema), length: 9 }, timeframe: tf }, { alias: 'slow', indicator: 'ema', params: { ...defaultParams(ema), length: 21 }, timeframe: tf }],
      long_entry: { type: 'compare', left: { kind: 'indicator', ref: 'fast' }, op: 'crosses_above', right: { kind: 'indicator', ref: 'slow' }, within: 1 },
      stop: { ...d.stop, mode: 'percent', value: 1 }, target: { ...d.target, mode: 'rr', value: 2 },
    });
  };
  const untouched = !draft.editing && !isPython && !isOptions && !d.indicators.length && !d.long_entry && !d.short_entry;

  const addPreset = async (key: 'nifty50' | 'watchlist') => {
    setInstError(null);
    try {
      const [universes, all] = await Promise.all([
        lab<{ presets: { key: string; symbols: string[]; excluded: { symbol: string }[] }[] }>('/lab/universes'),
        lab<InstrumentRow[]>(`/instruments?market=${d.market}&query=`),
      ]);
      const preset = universes.presets.find((p) => p.key === key);
      const wanted = new Set([...(preset?.symbols ?? []), ...(preset?.excluded ?? []).map((e) => e.symbol)]);
      const ids = all.filter((i) => wanted.has(i.symbol) && i.bars_1m > 0).map((i) => i.id);
      if (!ids.length) { setInstError('None of those stocks has stored 1-minute prices yet.'); return; }
      patch({ universe: [...new Set([...d.universe, ...ids])].slice(0, 200) });
    } catch (e) { setInstError(messageOf(e)); }
  };

  const errors = (validation?.issues ?? []).filter((x) => x.level === 'error');
  const warnings = (validation?.issues ?? []).filter((x) => x.level === 'warning');
  const canSave = !saving && validation?.valid === true && d.name.trim().length > 0 && d.universe.length > 0;
  const todo = [
    ...(d.name.trim() ? [] : ['give it a name']), ...(d.universe.length ? [] : ['choose at least one stock']),
    ...errors.filter((x) => !(x.where === 'name' || /^name /.test(x.message))).map((x) => x.message),
  ];
  const sentences = isPython || isOptions ? [] : readBack(d);

  const save = async () => {
    setSaving(true);
    setSaveError(null);
    try {
      const body = { definition: d, source_text: draft.sourceText };
      const row = draft.editing
        ? await lab<StrategyRow>(`/strategies/${draft.editing.id}/versions`, { method: 'POST', body })
        : await lab<StrategyRow>('/strategies', { method: 'POST', body });
      onSaved(row, draft.editing ? `Saved ${row.name} as version ${row.current_version}. Earlier versions are unchanged.` : `Created ${row.name} (suspended). Backtest it, then activate to monitor signals.`);
    } catch (e) {
      setSaveError(messageOf(e));
    } finally {
      setSaving(false);
    }
  };

  const toggleInstrument = (id: string) => patch({ universe: d.universe.includes(id) ? d.universe.filter((x) => x !== id) : [...d.universe, id] });
  const nameOf = (id: string) => instruments.find((i) => i.id === id)?.symbol ?? id.slice(0, 8);

  return (
    <div className="sl-builder">
      <div className="sl-toolbar">
        <div>
          <h2>{draft.editing ? `Editing ${draft.editing.name}` : isOptions ? 'New options strategy' : 'New intraday strategy'}</h2>
          <p className="sl-muted">{draft.editing ? `Saving creates version ${draft.editing.version + 1}; version ${draft.editing.version} stays exactly as it is.` : 'Built from 1-minute candles; higher timeframes are derived from completed candles only.'}</p>
        </div>
        <div className="sl-toolbar-actions">
          <Pill tone={d.source.type === 'visual' ? 'info' : 'warn'}>source: {d.source.type}</Pill>
          <button type="button" onClick={onReset}>Reset</button>
          <button type="button" className="sl-primary" disabled={!canSave} onClick={save}>{saving ? 'Saving…' : draft.editing ? `Save as version ${draft.editing.version + 1}` : 'Save strategy'}</button>
        </div>
      </div>
      {saveError && <Notice tone="bad">{saveError}</Notice>}
      {!canSave && !saving && todo.length > 0 && <p className="sl-todo" role="status">To save: {todo.join('; ')}.</p>}
      {warnings.map((x) => <Notice key={`${x.where}${x.message}`} tone="warn">{x.message}</Notice>)}
      {untouched && <div className="sl-toolbar"><button type="button" onClick={example}>Start from an example (EMA 9 / 21 cross)</button><span className="sl-muted">Or follow these: 1. add indicators, 2. say when to buy, 3. set a stop and target, 4. choose stocks.</span></div>}
      {sentences.length > 0 && <div className="sl-card sl-readback" aria-label="Your strategy in words">{sentences.map((s) => <p key={s}>{s}</p>)}</div>}
      {(d.source.provenance as { derived_from?: { name: string; version: number } }).derived_from && (
        <Notice tone="info">Customizing a copy of <strong>{(d.source.provenance as { derived_from: { name: string } }).derived_from.name}</strong> v{(d.source.provenance as { derived_from: { version: number } }).derived_from.version}. Saving creates a new strategy under the name above; the original and its versions are unchanged.</Notice>
      )}
      {d.source.type !== 'visual' && d.source.type !== 'starter' && (
        <Notice tone="warn">
          {isPython ? 'Logic comes from the uploaded Python file and cannot be edited here; indicators, sizing, risk and universe can.' : 'Translated from Pine Script. Edits here are recorded as changes after import. Equivalence with TradingView is not validated.'}
          {typeof d.source.artifact?.filename === 'string' && <> File: <code>{d.source.artifact.filename}</code>.</>}
        </Notice>
      )}

      <div className="sl-grid-2">
        <fieldset className="sl-card">
          <legend>Strategy</legend>
          <Field label="Name"><input value={d.name} maxLength={120} onChange={(e) => patch({ name: e.target.value })} placeholder="e.g. EMA 20/50 cross with VWAP filter" /></Field>
          <Field label="Description"><input value={d.description} maxLength={500} onChange={(e) => patch({ description: e.target.value })} /></Field>
          <div className="sl-row">
            <Field label="Decision timeframe" hint="Conditions are evaluated when a candle of this size completes">
              <select aria-label="Decision timeframe" disabled={isOptions} title={isOptions ? 'Options strategies decide on the entry timeframe set in the Options tab' : undefined} value={d.decision_timeframe} onChange={(e) => {
                const from = d.decision_timeframe; const to = e.target.value;
                // indicators and price operands that follow the evaluation timeframe move with it; deliberate higher-timeframe confirmations stay
                patch({ decision_timeframe: to, indicators: d.indicators.map((i) => (i.timeframe === from ? { ...i, timeframe: to } : i)),
                  long_entry: retimeCondition(d.long_entry, from, to), short_entry: retimeCondition(d.short_entry, from, to), long_exit: retimeCondition(d.long_exit, from, to), short_exit: retimeCondition(d.short_exit, from, to) });
              }}>{meta.timeframes.map((t) => <option key={t}>{t}</option>)}</select>
            </Field>
            <Field label="Base data"><input value="1m OHLCV" readOnly aria-readonly="true" /></Field>
          </div>
        </fieldset>
        <fieldset className="sl-card">
          <legend>Universe <span className="sl-muted">({d.universe.length} selected)</span></legend>
          <Field label="Search instruments"><input value={instQuery} onChange={(e) => setInstQuery(e.target.value)} placeholder="Symbol or name" /></Field>
          {instError && <Notice tone="bad">{instError}</Notice>}
          <div className="sl-toolbar"><button type="button" onClick={() => void addPreset('nifty50')}>Add Nifty 50</button><button type="button" onClick={() => void addPreset('watchlist')}>Add my watchlist</button>{d.universe.length > 0 && <button type="button" onClick={() => patch({ universe: [] })}>Clear</button>}</div>
          <div className="sl-chips" aria-label="Selected instruments">{d.universe.map((id) => <button type="button" key={id} className="sl-chip" onClick={() => toggleInstrument(id)} title="Remove">{nameOf(id)} ✕</button>)}</div>
          <Scroll label="Instrument results">
            <table className="sl-table sl-compact"><thead><tr><th>Symbol</th><th>1m bars</th><th>History</th><th /></tr></thead><tbody>
              {instruments.slice(0, 60).map((i) => (
                <tr key={i.id}><td>{i.symbol}</td><td className="sl-r">{i.bars_1m.toLocaleString()}</td>
                  <td>{i.bars_1m ? `${i.first_bar?.slice(0, 10)} → ${i.last_bar?.slice(0, 10)}` : <Pill tone="bad">no 1m data</Pill>}</td>
                  <td><button type="button" aria-label={`${d.universe.includes(i.id) ? 'Remove' : 'Add'} ${i.symbol}`} onClick={() => toggleInstrument(i.id)}>{d.universe.includes(i.id) ? 'Remove' : 'Add'}</button></td></tr>
              ))}
              {!instruments.length && <tr><td colSpan={4} className="sl-muted">No instruments with 1-minute data for this market{instQuery ? ' match your search' : ''}.</td></tr>}
            </tbody></table>
          </Scroll>
        </fieldset>
      </div>

      <nav className="sl-subtabs" aria-label="Builder sections">
        {([...(isOptions ? [['options', 'Options strategy']] as const : []), ['logic', 'Entry & exit logic'], ['indicators', `Indicators (${d.indicators.length})`], ['risk', 'Risk & simulation'], ['review', 'Validation']] as const).map(([k, label]) => (
          <button key={k} type="button" className={section === k ? 'active' : ''} aria-pressed={section === k} onClick={() => setSection(k)}>{label}{k === 'review' && errors.length > 0 ? ` · ${errors.length}` : ''}</button>
        ))}
      </nav>

      {section === 'options' && d.options && <OptionsPanel d={d} patch={patch} timeframes={meta.timeframes} instruments={instruments} />}

      {section === 'logic' && (
        <div className="sl-card">
          {isPython ? <Notice tone="info">Entry/exit logic is defined by the uploaded Python strategy.</Notice> : (
            <>
              <p className="sl-muted">Conditions are checked on completed {d.decision_timeframe} candles. Higher-timeframe operands become visible only after their own candle has closed.</p>
              {!d.indicators.length && <p className="sl-muted">Rules usually compare indicators. <button type="button" className="sl-link" onClick={() => setSection('indicators')}>Add indicators first</button>, or compare price and volume directly.</p>}
              {RULES.map(({ key, label }) => (
                <ConditionEditor key={key} title={label} value={d[key]} decls={d.indicators} meta={meta} timeframe={d.decision_timeframe}
                  onChange={(c) => patch({ [key]: c } as Partial<Definition>)} />
              ))}
            </>
          )}
        </div>
      )}

      {section === 'indicators' && (
        <div className="sl-card">
          <div className="sl-row sl-end">
            <Field label="Search"><input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="RSI, VWAP, supertrend…" /></Field>
            <Field label="Category"><select value={category} onChange={(e) => setCategory(e.target.value)}>{categories.map((c) => <option key={c}>{c}</option>)}</select></Field>
            <Field label="Indicator">
              <select aria-label="Indicator to add" value={pick || available[0]?.id || ''} onChange={(e) => setPick(e.target.value)}>
                {available.map((i) => <option key={i.id} value={i.id}>{i.name} ({i.category})</option>)}
              </select>
            </Field>
            <button type="button" className="sl-primary" onClick={addIndicator} disabled={!available.length}>Add indicator</button>
          </div>
          <Scroll label="Selected indicators">
            <table className="sl-table"><thead><tr><th>Alias</th><th>Indicator</th><th>Timeframe</th><th>Parameters</th><th /></tr></thead><tbody>
              {d.indicators.map((ind, i) => {
                const info = meta.indicators.find((x) => x.id === ind.indicator);
                return (
                  <tr key={ind.alias}>
                    <td><code>{ind.alias}</code></td><td title={info?.formula}>{info?.name ?? ind.indicator}</td>
                    <td><select aria-label={`${ind.alias} timeframe`} value={ind.timeframe} onChange={(e) => setIndicator(i, { ...ind, timeframe: e.target.value })}>{meta.timeframes.map((t) => <option key={t}>{t}</option>)}</select></td>
                    <td className="sl-params">{info && Object.entries(info.params).map(([name, p]) => (
                      <label key={name}>{name}
                        {p.kind === 'choice'
                          ? <select aria-label={`${ind.alias} ${name}`} value={String(ind.params[name] ?? p.default)} onChange={(e) => setIndicator(i, { ...ind, params: { ...ind.params, [name]: e.target.value } })}>{p.choices.map((c) => <option key={c}>{c}</option>)}</select>
                          : p.kind === 'text'
                            ? <input aria-label={`${ind.alias} ${name}`} value={String(ind.params[name] ?? '')} onChange={(e) => setIndicator(i, { ...ind, params: { ...ind.params, [name]: e.target.value } })} placeholder="ISO time" />
                            : <input aria-label={`${ind.alias} ${name}`} className="sl-num" type="number" step={p.kind === 'int' ? 1 : 'any'} min={p.min ?? undefined} max={p.max ?? undefined} value={Number(ind.params[name] ?? p.default)} onChange={(e) => setIndicator(i, { ...ind, params: { ...ind.params, [name]: Number(e.target.value) } })} />}
                      </label>
                    ))}</td>
                    <td><button type="button" className="sl-icon" aria-label={`Remove ${ind.alias}`} onClick={() => setIndicator(i, null)}>✕</button></td>
                  </tr>
                );
              })}
              {!d.indicators.length && <tr><td colSpan={5} className="sl-muted">No indicators yet. Price/volume comparisons work without any.</td></tr>}
            </tbody></table>
          </Scroll>
        </div>
      )}

      {section === 'risk' && (
        <div className="sl-grid-2">
          <fieldset className="sl-card"><legend>Position sizing (simulation)</legend>
            <div className="sl-row">
              <Field label="Method"><select value={d.sizing.method} onChange={(e) => patch({ sizing: { ...d.sizing, method: e.target.value } })}>
                <option value="percent_equity">% of equity</option><option value="fixed_notional">Fixed notional</option><option value="fixed_quantity">Fixed quantity</option><option value="risk_percent">Risk % of equity (needs stop)</option></select></Field>
              <Field label="Value"><input type="number" step="any" min={0} value={d.sizing.value} onChange={(e) => patch({ sizing: { ...d.sizing, value: Number(e.target.value) } })} /></Field>
            </div>
            <div className="sl-row">
              <label className="sl-check"><input type="checkbox" checked={d.constraints.allow_long} onChange={(e) => patch({ constraints: { ...d.constraints, allow_long: e.target.checked } })} /> Allow long</label>
              <label className="sl-check"><input type="checkbox" checked={d.constraints.allow_short} onChange={(e) => patch({ constraints: { ...d.constraints, allow_short: e.target.checked } })} /> Allow short</label>
              <Field label="Max trades / day"><input type="number" min={1} value={d.constraints.max_trades_per_day ?? ''} onChange={(e) => patch({ constraints: { ...d.constraints, max_trades_per_day: e.target.value ? Number(e.target.value) : null } })} /></Field>
            </div>
              <div className="sl-row">
                <Field label="No new entries after" hint="Default 15:00 IST"><input type="time" value={d.session.no_entry_after ?? ''} onChange={(e) => patch({ session: { ...d.session, no_entry_after: e.target.value || null } })} /></Field>
                <Field label="Square-off at" hint="Default 15:15 IST; positions never run overnight"><input type="time" value={d.session.square_off ?? ''} onChange={(e) => patch({ session: { ...d.session, square_off: e.target.value || null } })} /></Field>
              </div>
          </fieldset>
          <fieldset className="sl-card"><legend>Stop / target / trailing (historical simulation only)</legend>
            {(['stop', 'target', 'trail'] as const).map((k) => (
              <div className="sl-row" key={k}>
                <Field label={k === 'stop' ? 'Stop-loss' : k === 'target' ? 'Target' : 'Trailing stop'}>
                  <select aria-label={`${k} mode`} value={d[k].mode} onChange={(e) => patch({ [k]: { ...d[k], mode: e.target.value } } as Partial<Definition>)}>
                    <option value="none">None</option><option value="percent">Percent of entry</option><option value="atr">ATR multiple (1m)</option>{k === 'target' && <option value="rr">Risk/reward multiple</option>}
                  </select>
                </Field>
                <Field label={d[k].mode === 'percent' ? 'Percent' : d[k].mode === 'rr' ? 'R multiple' : d[k].mode === 'atr' ? 'ATR ×' : 'Value'}>
                  <input aria-label={`${k} value`} type="number" step="any" min={0} disabled={d[k].mode === 'none'} value={d[k].value} onChange={(e) => patch({ [k]: { ...d[k], value: Number(e.target.value) } } as Partial<Definition>)} />
                </Field>
                {d[k].mode === 'atr' && <Field label="ATR length"><input type="number" min={1} value={d[k].atr_length} onChange={(e) => patch({ [k]: { ...d[k], atr_length: Number(e.target.value) } } as Partial<Definition>)} /></Field>}
              </div>
            ))}
            <p className="sl-muted">These levels exist only inside backtests. Live signals never carry stop or target levels, and nothing here places an order.</p>
          </fieldset>
        </div>
      )}

      {section === 'review' && (
        <div className="sl-card" aria-label="Validation results">
          {!validation && <p className="sl-muted">Validating…</p>}
          {validation && (
            <>
              <p>{validation.valid ? <Pill tone="ok">valid</Pill> : <Pill tone="bad">{errors.length} error(s)</Pill>} {warnings.length > 0 && <Pill tone="warn">{warnings.length} warning(s)</Pill>}</p>
              <ul className="sl-issues">{validation.issues.map((x, i) => <li key={i} className={`sl-issue-${x.level}`}><strong>{x.level}</strong> {x.where && <code>{x.where}</code>} {x.message}</li>)}</ul>
              {validation.requirements && (
                <dl className="sl-dl">
                  <dt>Timeframes used</dt><dd>{validation.requirements.timeframes.join(', ')}</dd>
                  <dt>Warm-up needed</dt><dd>{validation.requirements.warmup_base_minutes.toLocaleString()} one-minute bars</dd>
                  <dt>Condition nodes</dt><dd>{validation.requirements.condition_nodes}</dd>
                  <dt>Definition hash</dt><dd><code>{validation.definition_hash?.slice(0, 16)}…</code></dd>
                </dl>
              )}
            </>
          )}
          {!isPython && (
            <div className="sl-summary" aria-label="Rule summary">
              <h4>Rule summary</h4>
              {RULES.map(({ key, label }) => d[key as RuleName] && <p key={key}><strong>{label}:</strong> <code>{describeCondition(d[key as RuleName]!, d.indicators)}</code></p>)}
            </div>
          )}
        </div>
      )}
      <p className="sl-disclaimer">{DISCLAIMER}</p>
    </div>
  );
}
