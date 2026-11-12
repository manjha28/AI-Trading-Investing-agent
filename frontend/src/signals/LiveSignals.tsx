import { Fragment, useCallback, useEffect, useState } from 'react';
import { DISCLAIMER, fmtTime, lab, messageOf } from './lab';
import { LifecycleChip } from './LifecycleChip';
import { GROUP_LABEL, STATE_META, STATE_ORDER, fmtIst, suppressionText, withTimeout, type ScanResult } from './lifecycle';
import { RejectionsPanel } from './RejectionsPanel';
import { SignalTimeline } from './SignalTimeline';
import { Empty, Notice, Pill, Scroll } from './shared';
import type { InstrumentRow, ScheduleView, SignalRow, StrategyRow } from './types';

const POLL_MS = 1_500;
const SCAN_WAIT_MS = 120_000;
const sleep = (ms: number) => new Promise<void>((resolve) => { setTimeout(resolve, ms); });

// Renders the backend's verdict (data_freshness.state, computed from the stored as-of timestamp at read time). No stale/fresh logic here.
function FreshnessCell({ f }: { f: Record<string, unknown> }) {
  const age = typeof f.age_minutes === 'number' ? f.age_minutes : null;
  if (f.state === 'fresh') return <><Pill tone="ok">fresh</Pill> {age} min</>;
  if (f.state === 'stale') return <><Pill tone="bad">stale</Pill> {age} min</>;
  return <Pill tone="warn">age unknown</Pill>;
}

export function LiveSignals({ strategies, instruments = [], onChanged }: { strategies: StrategyRow[]; instruments?: InstrumentRow[]; onChanged: () => void }) {
  const [rows, setRows] = useState<SignalRow[]>([]);
  const [status, setStatus] = useState('all');
  const [strategy, setStrategy] = useState('all');
  const [direction, setDirection] = useState('all');
  const [open, setOpen] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [lastScan, setLastScan] = useState<{ result: ScanResult; strategies: number; at: string } | null>(null);
  const [latestRecorded, setLatestRecorded] = useState<ScanResult | null>(null);
  const [schedule, setSchedule] = useState<ScheduleView | null>(null);
  const [showRejections, setShowRejections] = useState(false);
  const [rejectionRun, setRejectionRun] = useState<string | null>(null);
  const active = strategies.filter((s) => s.kind === 'intraday' && s.status === 'active');

  const load = useCallback(async () => {
    try {
      const q = new URLSearchParams();
      if (status !== 'all') q.set('status', status);
      if (strategy !== 'all') q.set('strategy_id', strategy);
      setRows(await lab<SignalRow[]>(`/signals?${q.toString()}`));
      setError(null);
    } catch (e) { setError(messageOf(e)); }
  }, [status, strategy]);

  useEffect(() => { void load(); }, [load]);
  // Scans run on the server. This page does not schedule anything: it reads the latest server-side run and schedule on load, on focus and on request.
  const loadServerState = useCallback(() => {
    withTimeout(lab<ScanResult[]>('/scan-runs?limit=1')).then((runs) => setLatestRecorded(runs[0] ?? null)).catch(() => setLatestRecorded(null));
    withTimeout(lab<ScheduleView>('/scanner/schedule')).then(setSchedule).catch(() => setSchedule(null));
  }, []);
  useEffect(() => {
    loadServerState();
    const onVisible = () => { if (document.visibilityState === 'visible') { loadServerState(); void load(); } };
    document.addEventListener('visibilitychange', onVisible);
    return () => document.removeEventListener('visibilitychange', onVisible);
  }, [loadServerState, load]);
  const scan = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      const queued = await withTimeout(lab<{ scan_run_id: string; strategies_queued: number; skipped_in_flight: string[] }>('/signals/scan', { method: 'POST', body: { strategy_ids: strategy === 'all' ? [] : [strategy] } }));
      if (queued.strategies_queued === 0) {
        setError(queued.skipped_in_flight.length ? `Already being scanned on the server: ${queued.skipped_in_flight.join(', ')}. Wait for it to finish.` : 'Nothing to scan: no active intraday strategy.');
        return;
      }
      const deadline = Date.now() + SCAN_WAIT_MS;
      for (;;) {
        const r = await withTimeout(lab<ScanResult>(`/scan-runs/${queued.scan_run_id}`));
        if (r.status !== 'running') {
          setLastScan({ result: r, strategies: r.strategies ?? queued.strategies_queued, at: new Date().toISOString() });
          setRejectionRun(r.scan_run_id);
          break;
        }
        if (Date.now() > deadline) { setError('The scan is still running on the server. It will finish on its own; see Jobs & scanner for its progress.'); break; }
        await sleep(POLL_MS);
      }
      await load();
      loadServerState();
      onChanged();
    } catch (e) { setError(messageOf(e)); } finally { setBusy(false); }
  }, [strategy, load, onChanged, loadServerState]);

  const shown = rows.filter((r) => direction === 'all' || r.direction === direction);
  return (
    <div className="sl-live">
      <div className="sl-filters" role="search" aria-label="Signal filters">
        <select aria-label="Signal status" value={status} onChange={(e) => setStatus(e.target.value)}>
          <option value="all">All statuses</option>
          {(['in_progress', 'resolved', 'ended'] as const).map((g) => (
            <optgroup key={g} label={GROUP_LABEL[g]}>{STATE_ORDER.filter((st) => STATE_META[st].group === g).map((st) => <option key={st} value={st}>{STATE_META[st].label}</option>)}</optgroup>
          ))}
          <optgroup label="Legacy aliases"><option value="new">new (= READY)</option><option value="suppressed">suppressed (see rejections)</option></optgroup>
        </select>
        <select aria-label="Signal strategy" value={strategy} onChange={(e) => setStrategy(e.target.value)}>
          <option value="all">All strategies</option>{strategies.filter((s) => s.kind === 'intraday').map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
        </select>
        <select aria-label="Signal direction" value={direction} onChange={(e) => setDirection(e.target.value)}><option value="all">Long + short</option><option value="long">Long</option><option value="short">Short</option></select>
        <button type="button" className="sl-primary" disabled={busy || !active.length} onClick={scan}>{busy ? 'Scanning…' : 'Scan now'}</button>
        <button type="button" onClick={() => { void load(); loadServerState(); }}>Refresh</button>
        <span className="sl-muted">{active.length} active strateg{active.length === 1 ? 'y' : 'ies'}</span>
      </div>
      <Notice tone="info">{DISCLAIMER} Signals are evaluated on completed candles from stored 1-minute data; none carry stop or target levels.</Notice>
      {error && <Notice tone="bad">{error}</Notice>}
      <p className="sl-muted" aria-label="Server-side scanning status">
        {schedule === null ? 'Server-side scanning status unavailable.' : schedule.scans_enabled
          ? <>Server-side scanning is <strong>on</strong>. {nextScan(schedule)}</>
          : <>Server-side scanning is <strong>off</strong>: nothing scans unless you press “Scan now” or switch it on under Jobs &amp; scanner.</>}
      </p>
      <ScanSummary scan={lastScan} latest={latestRecorded} onShowRejections={(run) => { setRejectionRun(run); setShowRejections(true); }} />
      {status === 'suppressed' && <Notice tone="info">Suppressed candidates are scan rejections, not signals. They are listed under “Suppressed candidates” below.</Notice>}
      {!shown.length ? (
        <Empty title={rows.length ? 'No signals match the direction filter' : 'No signals for the current filters'}>
          {active.length
            ? lastScan
              ? `The last scan found ${lastScan.result.new_signals} new signal(s) and ${suppressionText(lastScan.result.rejections)}. Suppressed pairs could not be evaluated, so “nothing triggered” is only true for the rest.`
              : 'Press “Scan now” to evaluate your active strategies. Nothing is shown until a scan has run or signals exist.'
            : 'Activate an intraday strategy in the Library to monitor it.'}
        </Empty>
      ) : (
        <Scroll label="Signals">
          <table className="sl-table">
            <thead><tr><th>Signal time</th><th>Instrument</th><th>Strategy</th><th>Dir.</th><th>TF</th><th className="sl-r">Reference</th><th>Status</th><th>Data</th><th>Expires</th></tr></thead>
            <tbody>
              {shown.map((s) => (
                <Fragment key={s.id}>
                  <tr className="sl-click" aria-expanded={open === s.id} onClick={() => setOpen(open === s.id ? null : s.id)}>
                    <td>{fmtTime(s.signal_time)}</td><td>{s.symbol}</td><td>{s.strategy_name} v{s.strategy_version}</td>
                    <td className={s.direction === 'long' ? 'sl-pos' : 'sl-neg'}>{s.direction}</td><td>{s.timeframe}</td>
                    <td className="sl-r">{s.reference_price} {s.currency}</td>
                    <td><LifecycleChip status={s.status} />{s.suppression_reason && <div className="sl-muted">{s.suppression_reason}</div>}</td>
                    <td><FreshnessCell f={s.data_freshness} /></td>
                    <td>{fmtTime(s.expires_at)}</td>
                  </tr>
                  {open === s.id && (
                    <tr className="sl-detail"><td colSpan={9}>
                      <p>{s.rationale}</p>
                      <SignalTimeline signal={s} onUpdated={(u) => { setRows((cur) => cur.map((x) => (x.id === u.id ? { ...x, status: u.status } : x))); onChanged(); }} />
                      {s.option && (
                        <section className="sl-card" aria-label="Option contract">
                          <h4>Option contract</h4>
                          {s.option.ok ? (
                            <dl className="sl-dl">
                              <dt>Underlying signal</dt><dd>{s.direction === 'long' ? 'Bullish' : 'Bearish'} · {s.symbol}{s.option.reference_label ? ` · ATM from ${s.option.reference_label}` : ''}</dd>
                              <dt>Selected option</dt><dd>{s.option.symbol} · {s.option.option_type} · strike {s.option.strike} · expiry {s.option.expiry} · lot {s.option.lot_size}</dd>
                              <dt>Delta</dt><dd>{s.option.delta == null ? '—' : s.option.delta.toFixed(3)} ({s.option.delta_source})</dd>
                              <dt>Bid / ask / spread</dt><dd>{s.option.bid} / {s.option.ask} / {s.option.spread_pct}% · quote {s.option.quote_time?.slice(11, 19)} UTC</dd>
                              <dt>Entry reference (ask)</dt><dd>{s.option.entry_reference_ask}</dd>
                              <dt>Premium stop / target</dt><dd>{s.option.premium_stop} / {s.option.premium_target} · max hold {s.option.max_hold_minutes} min (research levels, not orders)</dd>
                              <dt>Why this contract</dt><dd>{s.option.why}</dd>
                            </dl>
                          ) : (
                            <>
                              <Notice tone="warn">NO TRADE: {(s.option.no_trade_reasons ?? []).join(', ') || 'no eligible contract'}. A missing or stale quote is never shown as a valid price.</Notice>
                              {s.option.candidates && <ul className="sl-explain">{s.option.candidates.map((c, i) => <li key={i}>{c.strike_choice}{c.symbol ? ` ${c.symbol}` : ''}: {c.rejected.join(', ') || 'eligible'}</li>)}</ul>}
                            </>
                          )}
                        </section>
                      )}
                      <div className="sl-grid-2">
                        <div><h4>Conditions satisfied</h4><ul className="sl-explain">{s.conditions_met.map((c, i) => <li key={i} style={{ paddingLeft: c.depth * 12 }}>✓ {c.label}{c.left != null ? ` (${c.left} vs ${c.right})` : ''}</li>)}</ul>
                          {s.conditions_unmet.length > 0 && <><h4>Not satisfied</h4><ul className="sl-explain">{s.conditions_unmet.map((c, i) => <li key={i} className="sl-muted" style={{ paddingLeft: c.depth * 12 }}>✗ {c.label}{c.left != null ? ` (${c.left} vs ${c.right})` : ''}</li>)}</ul></>}</div>
                        <div><h4>Indicator values</h4><dl className="sl-dl">{Object.entries(s.indicators).map(([a, outs]) => <Fragment key={a}><dt>{a}</dt><dd>{Object.entries(outs).map(([o, v]) => `${o}: ${v ?? '—'}`).join(' · ')}</dd></Fragment>)}</dl>
                          <h4>Data freshness</h4><p className="sl-muted">Last candle closed {s.data_freshness.last_bar_close ? fmtTime(String(s.data_freshness.last_bar_close)) : '—'} · {String(s.data_freshness.bars_used ?? '—')} bars · quality {String(s.data_freshness.quality_status)}</p></div>
                      </div>
                    </td></tr>
                  )}
                </Fragment>
              ))}
            </tbody>
          </table>
        </Scroll>
      )}
      <p><button type="button" className="sl-link" aria-expanded={showRejections || status === 'suppressed'} onClick={() => setShowRejections(!showRejections)}>{showRejections || status === 'suppressed' ? 'Hide' : 'Show'} suppressed candidates</button></p>
      {(showRejections || status === 'suppressed') && <RejectionsPanel strategies={strategies} instruments={instruments} scanRunId={rejectionRun} />}
    </div>
  );
}

function nextScan(schedule: ScheduleView): string {
  const times = schedule.strategies.filter((x) => x.enabled && x.next_run_at).map((x) => x.next_run_at as string).sort();
  return times.length ? `Next scheduled scan: ${fmtIst(times[0])}.` : 'No strategy has a schedule enabled, or the market is closed until the next session.';
}

/** One line after every scan, and the explanation behind an empty list. Counts come from the server's scan-run record, never inferred. */
export function ScanSummary({ scan, latest, onShowRejections }: {
  scan: { result: ScanResult; strategies: number; at: string } | null; latest: ScanResult | null; onShowRejections: (scanRunId: string | null) => void;
}) {
  if (!scan) {
    if (!latest || !latest.scan_run_id) return <p className="sl-muted sl-scan-summary">No scan has been recorded yet.</p>;
    return (
      <p className="sl-muted sl-scan-summary">
        Most recent server scan ({latest.trigger === 'schedule' ? 'scheduled' : 'manual'}, {fmtIst(latest.started_at)}): {latest.pairs_scanned} pair{latest.pairs_scanned === 1 ? '' : 's'} across {latest.instruments_scanned} instrument{latest.instruments_scanned === 1 ? '' : 's'}, {latest.new_signals} new signal{latest.new_signals === 1 ? '' : 's'},{' '}
        {latest.rejections.total ? <button type="button" className="sl-link" onClick={() => onShowRejections(latest.scan_run_id)}>{suppressionText(latest.rejections)}</button> : '0 suppressed'}.
      </p>
    );
  }
  const { result, strategies } = scan;
  return (
    <Notice tone={result.errors.length ? 'warn' : 'ok'}>
      <p className="sl-scan-summary">
        <strong>{strategies}</strong> active strateg{strategies === 1 ? 'y' : 'ies'} · <strong>{result.instruments_scanned}</strong> instrument{result.instruments_scanned === 1 ? '' : 's'} ({result.pairs_scanned} strategy/instrument pair{result.pairs_scanned === 1 ? '' : 's'}) scanned · <strong>{result.new_signals}</strong> new signal{result.new_signals === 1 ? '' : 's'} ·{' '}
        {result.rejections.total > 0
          ? <button type="button" className="sl-link" onClick={() => onShowRejections(result.scan_run_id)}>{suppressionText(result.rejections)}</button>
          : <span>0 suppressed</span>}
      </p>
      {result.errors.length > 0 && <ul>{result.errors.map((x, i) => <li key={i}>{x.strategy}{x.instrument ? ` / ${x.instrument}` : ''}: {x.error}</li>)}</ul>}
    </Notice>
  );
}
