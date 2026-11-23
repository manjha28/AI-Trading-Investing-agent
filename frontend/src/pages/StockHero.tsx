import { cardState } from '../lib/cardState';
import { Ladder, Ring, WinLine, inr, when, type Confidence, type Levels } from './DailyReport';
import type { Brief, HorizonKey, Instrument } from './researchTypes';
import { formatIST } from '../lib/formatIST';

const ORDER: HorizonKey[] = ['swing', 'positional', 'long_term'];
const NAME: Record<HorizonKey, string> = { swing: 'Swing', positional: 'Positional', long_term: 'Long-term' };
const SPAN: Record<HorizonKey, string> = { swing: 'days to weeks', positional: 'weeks to months', long_term: 'months to years' };
const REC: Record<string, { text: string; cls: string }> = {
  BUY: { text: 'Buy', cls: 'good' }, HOLD: { text: 'Hold', cls: 'info' }, AVOID: { text: 'Avoid', cls: 'bad' }, WAIT: { text: 'Wait', cls: 'mid' }, INSUFFICIENT_EVIDENCE: { text: 'Not enough data', cls: 'mute' },
};

type Props = {
  instrument: Instrument; brief: Brief | null; horizon: HorizonKey; onHorizon: (h: HorizonKey) => void; busy: boolean; watched: boolean;
  onRun: () => void; onRefresh: () => void; onToggleWatch: () => void; onJump: (tab: string) => void;
  loading?: boolean; loadError?: string | null; onRetry?: () => void;
};

/** Top of the stock page: who it is, the verdict on each horizon, and (for a buy) confidence and levels. Numbers come from the stored report only. */
export function StockHero({ instrument, brief, horizon, onHorizon, busy, watched, onRun, onRefresh, onToggleWatch, onJump, loading = false, loadError = null, onRetry }: Props) {
  const sections = brief?.sections as (Brief['sections'] & { levels?: { horizons?: Record<string, Levels> }; buy_confidence?: Record<string, Confidence>; market_data_recovery?: { history?: { reason?: string | null; bars_before?: number | null; bars_after?: number | null }; latest_candle?: { reason?: string | null; persisted?: boolean | null } } }) | undefined;
  const assessment = sections?.assessment && !sections.assessment.error ? sections.assessment : undefined;
  const current = sections?.technicals?.status === 'available';
  const close = sections?.technicals?.latest_close;
  const levels = sections?.levels?.horizons?.[horizon];
  const conf = sections?.buy_confidence?.[horizon];
  const active = assessment?.horizons[horizon];
  return <section className="rt-stock" aria-label="Stock summary">
    <header className="rt-stock-head">
      <div className="rt-stock-id">
        <h2>{instrument.symbol}</h2>
        <span className="rt-name">{instrument.display_name} · {instrument.venue}{instrument.is_synthetic ? ' · synthetic' : ''}</span>
        <div className="rt-chips">
          {brief && <span className={`rt-chip ${current ? 'rt-chip-ok' : 'rt-chip-warn'}`}>{current ? 'Price data current' : `Price data ${(sections?.technicals?.status ?? 'unavailable').replaceAll('_', ' ')}`}</span>}
          <span className="rt-chip">Researched {when(brief?.finished_at ?? brief?.created_at ?? null)}</span>
        </div>
      </div>
      <div className="rt-stock-price">{close ? <><small>Last close</small><b>{inr(close)}</b></> : null}</div>
      <div className="rt-stock-actions">
        <button className="rt-btn rt-primary" type="button" onClick={onRun} disabled={busy}>{busy ? 'Researching…' : brief ? 'Re-run research' : 'Run research'}</button>
        {!instrument.is_synthetic && <button className="rt-btn rt-ghost" type="button" onClick={onRefresh} disabled={busy}>Refresh price data</button>}
        <button className="rt-btn rt-ghost" type="button" onClick={onToggleWatch}>{watched ? 'Unwatch' : 'Watch'}</button>
      </div>
    </header>
    {brief && (sections as unknown as { technical_engine?: { horizons?: Record<string, { status: string; direction?: string; technical_score?: number; market_regime?: string }> } })?.technical_engine?.horizons?.[horizon]?.status === 'available' && <p className="rt-note">Technical read ({NAME[horizon]}): <b>{(((sections as unknown as { technical_engine: { horizons: Record<string, { direction?: string }> } }).technical_engine.horizons[horizon].direction) ?? '').replace('_', ' ').toLowerCase()}</b>, regime {(((sections as unknown as { technical_engine: { horizons: Record<string, { market_regime?: string }> } }).technical_engine.horizons[horizon].market_regime) ?? '').replace(/_/g, ' ').toLowerCase()}. Full technical evidence is in the Technicals tab below.</p>}
    {brief && !current && (sections?.market_data_recovery?.latest_candle?.reason || sections?.market_data_recovery?.history?.reason) && <p className="rt-alert" role="status">
      <b>Why price data is not current:</b> {sections?.market_data_recovery?.latest_candle?.reason ?? sections?.market_data_recovery?.history?.reason} Re-run research or use Refresh price data to retry.</p>}
    {!brief && loading && <div className="rt-card rt-empty-state" role="status" aria-busy="true"><p>Loading the research report…</p></div>}
    {!brief && !loading && (loadError || instrument.card_researched_at) && <div className="rt-card rt-empty-state" role="alert"><h3>Could not load the report (network or server error).</h3>
      <p>{instrument.card_researched_at ? `Researched ${formatIST(instrument.card_researched_at)}, but the report could not be loaded. ` : ''}{loadError ?? 'The server returned no report for this research run.'}</p>{onRetry && <button className="rt-btn" type="button" onClick={onRetry}>Retry</button>}</div>}
    {!brief && !loading && !loadError && !instrument.card_researched_at && <div className="rt-card rt-empty-state"><h3>No research report yet</h3><p>Run research to score {instrument.symbol} on swing, positional and long-term horizons and see entry levels if it is a buy.</p></div>}
    {brief && !assessment && <div className="rt-card rt-empty-state"><h3>No scoring in this report</h3><p>{sections?.assessment?.error ?? 'This report predates the scoring engine. Re-run research.'}</p></div>}
    {assessment && <>
      <div className="rt-hz" role="tablist" aria-label="Investment horizon">
        {ORDER.map((key) => {
          const h = assessment.horizons[key]; const rec = REC[h.recommendation] ?? REC.INSUFFICIENT_EVIDENCE;
          return <button key={key} type="button" role="tab" aria-selected={horizon === key} className={`rt-hz-card rt-hz-${rec.cls}${horizon === key ? ' active' : ''}`} onClick={() => onHorizon(key)}>
            <small>{NAME[key]} <i>{SPAN[key]}</i></small><b>{rec.text}</b><span>{h.score === null ? 'No score' : `Score ${Math.round(h.score)}`}{h.confidence === null ? '' : ` · evidence ${Math.round(h.confidence)}`}</span>
          </button>;
        })}
      </div>
      <div className="rt-stock-detail">
        <div className="rt-card rt-verdict-card">
          <h3>{NAME[horizon]} view: {(REC[active?.recommendation ?? ''] ?? REC.INSUFFICIENT_EVIDENCE).text}</h3>
          {active?.recommendation === 'BUY' && conf && <div className="rt-conf-row"><Ring score={conf.score} band={conf.band} /><div><b>{conf.band} confidence</b><p className="rt-note">{conf.note}</p><WinLine conf={conf} /></div></div>}
          {conf && <ul className="rt-parts">{conf.parts.map((p) => <li key={p.key}><span>{p.label}</span><div className="rt-bar"><i style={{ width: `${p.value}%` }} /></div><b>{Math.round(p.value)}</b></li>)}</ul>}
          {active && active.recommendation !== 'BUY' && <ul className="rt-why-not">{(active.blockers.length ? active.blockers : active.gates.map((g) => g.detail)).slice(0, 3).map((x) => <li key={x}>{x}</li>)}
            {active.blockers.length === 0 && active.gates.length === 0 && <li>The combined score does not support a new purchase on this horizon.</li>}</ul>}
          <button type="button" className="rt-link" onClick={() => onJump('Summary')}>See scoring breakdown ↓</button>
        </div>
        <div className="rt-card rt-levels-card">
          <h3>Buy levels · {NAME[horizon]}</h3>
          {active?.recommendation !== 'BUY' ? <p className="rt-empty">Levels are shown only when the horizon is a buy.</p>
            : levels?.status === 'available' ? <><Ladder levels={levels} /><p className="rt-note"><span className={`rt-chip ${cardState(levels).kind === 'in_band' ? 'rt-chip-ok' : 'rt-chip-warn'}`} title={cardState(levels).reason ?? undefined}>{cardState(levels).label}</span> {(levels.selected_setup ?? '').replace(/_/g, ' ').toLowerCase()}{levels.breakout_trigger ? ` · trigger ${inr(levels.breakout_trigger)}` : ''} · last close {inr(levels.reference_close)}</p>
              {levels.explanation && <p className="rt-note">{levels.explanation}</p>}

              {levels.derivation && <details className="rt-fold"><summary>How these levels are derived</summary><p className="rt-note">{levels.derivation.rule}</p></details>}</>
              : <p className="rt-empty">{levels?.reason ?? 'No levels for this horizon.'}</p>}
        </div>
      </div>
    </>}
    <p className="rt-disclaimer">Decision support, not investment advice. Levels are not orders.</p>
  </section>;
}
