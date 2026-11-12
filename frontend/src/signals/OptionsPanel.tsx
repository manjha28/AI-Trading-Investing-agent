import { useEffect, useState } from 'react';
import { lab, messageOf } from './lab';
import { Field, Notice, Pill, Scroll, StatusPill } from './shared';
import type { Definition, InstrumentRow, OptionsCoverage, OptionsSpec } from './types';

type Props = { d: Definition; patch: (c: Partial<Definition>) => void; timeframes: string[]; instruments: InstrumentRow[] };

const num = (v: string, fallback = 0) => (v === '' || Number.isNaN(Number(v)) ? fallback : Number(v));

export function OptionsCoveragePanel({ underlying = 'NIFTY' }: { underlying?: string }) {
  const [cov, setCov] = useState<OptionsCoverage | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => { let live = true; lab<OptionsCoverage>(`/options/coverage?underlying=${encodeURIComponent(underlying)}`).then((c) => live && setCov(c)).catch((e) => live && setErr(messageOf(e))); return () => { live = false; }; }, [underlying]);
  if (err) return <Notice tone="bad">{err}</Notice>;
  if (!cov) return <p className="sl-muted">Measuring {underlying} options data coverage…</p>;
  return (
    <section className="sl-card" aria-label="Options data coverage">
      <header className="sl-card-head"><h3>{underlying} options data coverage</h3>
        <StatusPill status={cov.backtest_mode === 'unavailable' ? 'no_data' : cov.backtest_mode === 'quote_accurate' ? 'ok' : 'warn'} />
      </header>
      <p>{cov.backtest_mode === 'quote_accurate' ? 'Quote-accurate options backtests are possible.' : cov.backtest_mode === 'indicative_ohlc' ? 'Only indicative OHLC option backtests are possible (not executable quotes).' : 'Options backtesting is not available: required data is missing.'}</p>
      {cov.blockers.length > 0 && <ul className="sl-issues" aria-label="Blockers">{cov.blockers.map((b) => <li key={b} className="sl-issue-error">{b}</li>)}</ul>}
      <Scroll label="Dataset coverage">
        <table className="sl-table sl-compact"><thead><tr><th>Dataset</th><th>Provider</th><th>Status</th><th className="sl-r">Rows</th><th>From</th><th>To</th></tr></thead><tbody>
          {Object.entries(cov.datasets).map(([k, v]) => <tr key={k}><td>{v.label}</td><td>{v.provider ?? '—'}</td><td><StatusPill status={v.status === 'available' ? 'ok' : 'no_data'} /></td><td className="sl-r">{v.rows ?? 0}</td><td>{v.first?.slice(0, 16) ?? '—'}</td><td>{v.last?.slice(0, 16) ?? '—'}</td></tr>)}
        </tbody></table>
      </Scroll>
      <p className="sl-muted">Live: {cov.live.option_chain_api}. {cov.live.note} Stored quote freshness: {cov.live.stored_quote_freshness_seconds == null ? 'no stored quotes' : `${Math.round(cov.live.stored_quote_freshness_seconds)} s`}.</p>
      <p className="sl-muted">{cov.disclaimer}</p>
    </section>
  );
}

export function OptionsPanel({ d, patch, timeframes, instruments }: Props) {
  const o = d.options as OptionsSpec;
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ tone: 'ok' | 'bad'; text: string } | null>(null);
  const set = <K extends keyof OptionsSpec>(group: K, changes: Partial<OptionsSpec[K]>) => patch({ options: { ...o, [group]: { ...(o[group] as object), ...changes } } as OptionsSpec });
  const u = o.underlying;
  const regenerate = async () => {
    setBusy(true); setMsg(null);
    try {
      const rules = await lab<Pick<Definition, 'indicators' | 'long_entry' | 'short_entry' | 'long_exit' | 'short_exit'>>('/options/compile', { method: 'POST', body: u });
      patch({ ...rules, decision_timeframe: u.entry_timeframe });
      setMsg({ tone: 'ok', text: 'Indicators and entry/exit rules were regenerated from these settings. Any manual edits to them were replaced.' });
    } catch (e) { setMsg({ tone: 'bad', text: messageOf(e) }); } finally { setBusy(false); }
  };
  const futures = instruments.filter((i) => i.market === 'equity_in');
  return (
    <div className="sl-options" aria-label="Options strategy configuration">
      <Notice tone="warn">Options buying research. Backtests need real contracts, quotes and Greeks and are refused when they are missing. Decision support only; nothing here places orders.</Notice>
      <div className="sl-grid-2">
        <fieldset className="sl-card"><legend>1 · Underlying signal</legend>
          <div className="sl-row">
            <Field label="Underlying"><input aria-label="Underlying symbol" value={u.underlying} onChange={(e) => set('underlying', { underlying: e.target.value.toUpperCase() })} /></Field>
            <Field label="Signal source" hint="Futures carry real traded volume"><select aria-label="Signal source" value={u.signal_source} onChange={(e) => set('underlying', { signal_source: e.target.value as 'futures' | 'spot' })}><option value="futures">Futures (price + volume)</option><option value="spot">Spot index</option></select></Field>
            <Field label="Spot index (ATM mapping)"><select aria-label="Spot instrument" value={u.spot_instrument_id ?? ''} onChange={(e) => set('underlying', { spot_instrument_id: e.target.value || null })}><option value="">None: futures price proxy</option>{futures.map((i) => <option key={i.id} value={i.id}>{i.symbol}</option>)}</select></Field>
          </div>
          <div className="sl-row">
            <Field label="Trend timeframe"><select aria-label="Trend timeframe" value={u.trend_timeframe} onChange={(e) => set('underlying', { trend_timeframe: e.target.value })}>{timeframes.map((t) => <option key={t}>{t}</option>)}</select></Field>
            <Field label="Entry timeframe"><select aria-label="Entry timeframe" value={u.entry_timeframe} onChange={(e) => { set('underlying', { entry_timeframe: e.target.value }); patch({ decision_timeframe: e.target.value }); }}>{timeframes.map((t) => <option key={t}>{t}</option>)}</select></Field>
            <Field label="Window start (IST)"><input aria-label="Window start" type="time" value={u.start_time} onChange={(e) => set('underlying', { start_time: e.target.value })} /></Field>
            <Field label="Last new entry (IST)"><input aria-label="Last entry" type="time" value={u.last_entry_time} onChange={(e) => set('underlying', { last_entry_time: e.target.value })} /></Field>
          </div>
          <div className="sl-row">
            {([['ema_fast', 'EMA fast'], ['ema_slow', 'EMA slow'], ['ema_pullback_alt', 'EMA pullback alt'], ['ema_slope_bars', 'EMA slope bars'], ['adx_length', 'ADX length'], ['adx_min', 'ADX min'], ['rsi_length', 'RSI length'],
              ['rsi_long_min', 'RSI long >'], ['rsi_short_max', 'RSI short <'], ['atr_length', 'ATR length'], ['pullback_atr', 'Pullback ×ATR'], ['pullback_lookback', 'Pullback look-back bars'], ['volume_length', 'Volume avg bars'],
              ['volume_multiple', 'Volume ×avg'], ['max_candle_range_atr', 'Max candle ×ATR'], ['max_vwap_extension_atr', 'Max VWAP ext ×ATR'], ['swing_left', 'Swing left'], ['swing_right', 'Swing right']] as const).map(([k, label]) => (
              <Field key={k} label={label}><input aria-label={label} className="sl-num" type="number" step="any" value={u[k]} onChange={(e) => set('underlying', { [k]: num(e.target.value) } as Partial<OptionsSpec['underlying']>)} /></Field>))}
          </div>
          <button type="button" className="sl-primary" disabled={busy} onClick={regenerate}>{busy ? 'Regenerating…' : 'Regenerate rules from these settings'}</button>
          {msg && <Notice tone={msg.tone}>{msg.text}</Notice>}
          <p className="sl-muted">The generated indicators and conditions are ordinary rules: fine-tune them in “Entry &amp; exit logic”. A regeneration overwrites those manual edits.</p>
        </fieldset>
        <fieldset className="sl-card"><legend>2 · Contract selection</legend>
          <div className="sl-row">
            <label className="sl-check"><input type="checkbox" aria-label="Allow CE" checked={o.selection.allow_ce} onChange={(e) => set('selection', { allow_ce: e.target.checked })} /> Buy CE (bullish)</label>
            <label className="sl-check"><input type="checkbox" aria-label="Allow PE" checked={o.selection.allow_pe} onChange={(e) => set('selection', { allow_pe: e.target.checked })} /> Buy PE (bearish)</label>
            <label className="sl-check"><input type="checkbox" aria-label="ATM" checked={o.selection.strike_choices.includes('atm')} onChange={(e) => set('selection', { strike_choices: e.target.checked ? Array.from(new Set([...o.selection.strike_choices, 'atm'])) as ('atm' | 'itm1')[] : o.selection.strike_choices.filter((x) => x !== 'atm') })} /> ATM</label>
            <label className="sl-check"><input type="checkbox" aria-label="ITM 1" checked={o.selection.strike_choices.includes('itm1')} onChange={(e) => set('selection', { strike_choices: e.target.checked ? Array.from(new Set([...o.selection.strike_choices, 'itm1'])) as ('atm' | 'itm1')[] : o.selection.strike_choices.filter((x) => x !== 'itm1') })} /> One strike ITM</label>
          </div>
          <div className="sl-row">
            {([['delta_min', 'Min |delta|'], ['delta_max', 'Max |delta|'], ['delta_preferred', 'Preferred |delta|'], ['expiry_rank', 'Expiry rank (0 = nearest)'], ['max_spread_pct', 'Max spread % of mid'], ['quote_max_age_seconds', 'Quote max age (s)'],
              ['min_volume', 'Min volume'], ['min_depth_lots', 'Min depth (lots)'], ['risk_free_rate_pct', 'Risk-free % (calc delta)']] as const).map(([k, label]) => (
              <Field key={k} label={label}><input aria-label={label} className="sl-num" type="number" step="any" value={o.selection[k]} onChange={(e) => set('selection', { [k]: num(e.target.value) } as Partial<OptionsSpec['selection']>)} /></Field>))}
          </div>
          <div className="sl-row">
            <label className="sl-check"><input type="checkbox" aria-label="Allow expiry day" checked={o.selection.allow_expiry_day} onChange={(e) => set('selection', { allow_expiry_day: e.target.checked })} /> Allow same-day expiry (off by default)</label>
            <label className="sl-check"><input type="checkbox" aria-label="Allow calculated Greeks" checked={o.selection.allow_calculated_greeks} onChange={(e) => set('selection', { allow_calculated_greeks: e.target.checked })} /> Allow delta calculated from a contemporaneous IV (labelled)</label>
          </div>
          <p className="sl-muted">Lot size, strike spacing and expiries come from the effective-dated contract master, never from code. Missing quote, depth, delta or lot size means NO TRADE.</p>
        </fieldset>
      </div>
      <div className="sl-grid-2">
        <fieldset className="sl-card"><legend>3 · Execution &amp; exits</legend>
          <div className="sl-row">
            <Field label="Price basis"><select aria-label="Price mode" value={o.execution.price_mode} onChange={(e) => set('execution', { price_mode: e.target.value as 'quote_accurate' | 'indicative_ohlc' })}><option value="quote_accurate">Quote-accurate (ask in / bid out)</option><option value="indicative_ohlc">Indicative OHLC (labelled, not executable)</option></select></Field>
            {([['premium_stop_pct', 'Premium stop %'], ['target_r', 'Target (R multiple)'], ['max_hold_minutes', 'Max hold (min)'], ['trailing_activation_r', 'Trail activation (R)'], ['trailing_distance_pct', 'Trail distance %'],
              ['slippage_ticks', 'Slippage (ticks)'], ['tick_size', 'Tick size'], ['entry_latency_seconds', 'Entry latency (s)'], ['max_entry_wait_seconds', 'Max entry wait (s)'], ['ohlc_half_spread_pct', 'OHLC half-spread %']] as const).map(([k, label]) => (
              <Field key={k} label={label}><input aria-label={label} className="sl-num" type="number" step="any" value={o.execution[k] ?? ''} onChange={(e) => set('execution', { [k]: e.target.value === '' ? null : Number(e.target.value) } as Partial<OptionsSpec['execution']>)} /></Field>))}
          </div>
          <div className="sl-row">
            <label className="sl-check"><input type="checkbox" aria-label="Underlying invalidation" checked={o.execution.underlying_invalidation} onChange={(e) => set('execution', { underlying_invalidation: e.target.checked })} /> Exit on underlying swing break</label>
            <label className="sl-check"><input type="checkbox" aria-label="Opposite signal exit" checked={o.execution.opposite_signal_exit} onChange={(e) => set('execution', { opposite_signal_exit: e.target.checked })} /> Exit on opposite signal</label>
            <label className="sl-check"><input type="checkbox" aria-label="Thesis exit" checked={o.execution.thesis_exit} onChange={(e) => set('execution', { thesis_exit: e.target.checked })} /> Exit when the 5m thesis fails</label>
          </div>
          <p className="sl-muted">Premium stop {o.execution.premium_stop_pct}% · target {(o.execution.premium_stop_pct * o.execution.target_r).toFixed(2)}% ({o.execution.target_r}R). If stop and target touch in one candle the stop is assumed first. Fee schedule <code>{o.execution.fee_schedule}</code> (options rates are unverified defaults).</p>
        </fieldset>
        <fieldset className="sl-card"><legend>4 · Risk management</legend>
          <div className="sl-row">
            <Field label="Sizing"><select aria-label="Sizing method" value={o.risk.sizing_method} onChange={(e) => set('risk', { sizing_method: e.target.value as 'risk_percent' | 'fixed_lots' })}><option value="risk_percent">Risk % of capital (whole lots)</option><option value="fixed_lots">Fixed lots</option></select></Field>
            {([['risk_per_trade_pct', 'Risk per trade %'], ['daily_loss_limit_pct', 'Daily loss limit %'], ['fixed_lots', 'Fixed lots']] as const).map(([k, label]) => (
              <Field key={k} label={label}><input aria-label={label} className="sl-num" type="number" step="any" value={o.risk[k]} onChange={(e) => set('risk', { [k]: num(e.target.value) } as Partial<OptionsSpec['risk']>)} /></Field>))}
            <Field label="Max positions"><input aria-label="Max positions" value="1 (overlap not supported)" readOnly /></Field>
          </div>
          <p className="sl-muted">Enforced by the backtester and the signal logic: a single lot above the risk budget is rejected; the daily loss limit blocks further entries that day; one position at a time.</p>
          <h4>Event blackouts</h4>
          {o.event_blackouts.map((b, i) => (
            <div className="sl-row" key={i}>
              <input aria-label={`Blackout ${i + 1} start`} value={b.start} onChange={(e) => patch({ options: { ...o, event_blackouts: o.event_blackouts.map((x, j) => (j === i ? { ...x, start: e.target.value } : x)) } })} placeholder="2026-02-01T09:00:00+05:30" />
              <input aria-label={`Blackout ${i + 1} end`} value={b.end} onChange={(e) => patch({ options: { ...o, event_blackouts: o.event_blackouts.map((x, j) => (j === i ? { ...x, end: e.target.value } : x)) } })} />
              <input aria-label={`Blackout ${i + 1} label`} value={b.label} onChange={(e) => patch({ options: { ...o, event_blackouts: o.event_blackouts.map((x, j) => (j === i ? { ...x, label: e.target.value } : x)) } })} placeholder="Label" />
              <button type="button" className="sl-icon" aria-label={`Remove blackout ${i + 1}`} onClick={() => patch({ options: { ...o, event_blackouts: o.event_blackouts.filter((_, j) => j !== i) } })}>✕</button>
            </div>))}
          <button type="button" onClick={() => patch({ options: { ...o, event_blackouts: [...o.event_blackouts, { start: '', end: '', label: '' }] } })}>+ Event blackout</button>
          <p className="sl-muted">No event calendar is supplied or invented: add the windows you want blocked.</p>
        </fieldset>
      </div>
      <OptionsCoveragePanel underlying={u.underlying} />
      <Pill tone="info">{d.options?.execution.price_mode === 'indicative_ohlc' ? 'indicative OHLC mode' : 'quote-accurate mode'}</Pill>
    </div>
  );
}
