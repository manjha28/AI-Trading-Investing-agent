import { useEffect, useMemo, useState, type KeyboardEvent } from 'react';
import { ApiRequestError, apiRequest } from '../api';
import { formatChange } from '../lib/formatChange';
import { formatMoney } from '../lib/formatMoney';
import { CandleChart, sessionCloseForVenue } from '../components/CandleChart';
import type {
  AiAnalysis, AiStatus, AiValuationView, Assessment, Shareholding, PeerMetric, Valuation, ValuationMetric, Brief, ComponentKey, FundamentalPeriod, HorizonKey, Instrument, QualityItem, ScoreComponent,
  SentimentEvent, SentimentHistoryPoint,
} from './researchTypes';

const HORIZON_ORDER: HorizonKey[] = ['swing', 'positional', 'long_term'];
const HORIZON_LABEL: Record<HorizonKey, string> = { swing: 'Swing', positional: 'Positional', long_term: 'Long-term' };
const COMPONENT_ORDER: ComponentKey[] = ['fundamental', 'financial_trends', 'technical', 'news_sentiment', 'valuation'];
// Briefs made before model 0.2.0 have no valuation component: show only the components the stored assessment really has.
const presentKeys = (assessment: Assessment) => COMPONENT_ORDER.filter((key) => assessment.components[key]);
const TABS = ['Overview', 'Fundamentals', 'Valuation', 'Shareholding', 'Financial Statements', 'Technical Analysis', 'News', 'Sentiment', 'Risks & Scenarios', 'Data Quality / Sources'] as const;
type Tab = (typeof TABS)[number];

export function plainText(value: string | null | undefined): string {
  if (!value) return '';
  const doc = new DOMParser().parseFromString(value, 'text/html');
  doc.querySelectorAll('script,style').forEach((node) => node.remove());
  return (doc.body.textContent ?? '').replace(/\s+/g, ' ').trim();
}

const num = (value: string | number | null | undefined, digits = 2) => {
  if (value === null || value === undefined || value === '') return '—';
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed.toLocaleString('en-IN', { maximumFractionDigits: digits }) : '—';
};
const ist = (value: string | null | undefined) => value
  ? new Intl.DateTimeFormat('en-IN', { dateStyle: 'medium', timeStyle: 'short', timeZone: 'Asia/Kolkata' }).format(new Date(value)) + ' IST'
  : '—';
const words = (value: string) => value.replaceAll('_', ' ');
const tone = (score: number | null) => score === null ? 'none' : score >= 65 ? 'good' : score >= 45 ? 'mid' : 'bad';
const REC_TEXT: Record<string, string> = {
  BUY: 'Buy-leaning', HOLD: 'Hold', AVOID: 'Avoid', WAIT: 'Wait', INSUFFICIENT_EVIDENCE: 'Insufficient evidence',
};

function ScoreBar({ score }: { score: number | null }) {
  return <div className={`rt-bar rt-${tone(score)}`} role="img" aria-label={score === null ? 'No score' : `Score ${score} of 100`}>
    {score != null && <span style={{ width: `${score}%` }} />}
  </div>;
}

function Reasons({ title, items, kind }: { title: string; items: string[]; kind: 'block' | 'warn' | 'info' }) {
  if (!items.length) return null;
  return <div className={`rt-reasons rt-reasons-${kind}`}><strong>{title}</strong><ul>{items.map((item) => <li key={item}>{item}</li>)}</ul></div>;
}

function ComponentCard({ component, weight }: { component: ScoreComponent; weight: number | undefined }) {
  const scored = component.status === 'scored';
  return <article className="rt-card rt-component" aria-label={component.label}>
    <header><h4>{component.label}</h4>{weight !== undefined && <small>weight {Math.round(weight * 100)}%</small>}</header>
    {scored ? <>
      <div className="rt-score-row"><strong className={`rt-score rt-${tone(component.score)}`}>{num(component.score, 1)}</strong><span>/100 · {component.interpretation}</span></div>
      <ScoreBar score={component.score} />
      <dl className="rt-meta"><div><dt>Confidence</dt><dd>{num(component.confidence, 0)}%</dd></div><div><dt>Data coverage</dt><dd>{num(component.coverage_pct, 0)}%</dd></div></dl>
      <ul className="rt-factors">{component.factors.slice(0, 4).map((f) => <li key={f.key}><span>{f.label}</span><b>{num(f.value, 2)}{f.unit && f.unit !== '' && !f.unit.startsWith('(') && !f.unit.startsWith('of') ? ` ${f.unit}` : ''}</b></li>)}</ul>
    </> : <div className="rt-blocked"><strong>Not scored</strong><span>Coverage {component.coverage_pct === null ? 'unknown' : `${num(component.coverage_pct, 0)}%`}</span></div>}
    {component.blockers.length > 0 && <details className="rt-blocked-why"><summary>Blocked: {component.blockers[0]}</summary>
      {component.blockers.length > 1 && <ul>{component.blockers.slice(1).map((b) => <li key={b}>{b}</li>)}</ul>}</details>}
    <Reasons title="Conflicting evidence" items={component.conflicts} kind="warn" />
    <Reasons title="Missing evidence" items={component.missing.slice(0, 3)} kind="info" />
    {component.missing.length > 3 && <details className="rt-more"><summary>{component.missing.length - 3} more missing items</summary><ul>{component.missing.slice(3).map((m) => <li key={m}>{m}</li>)}</ul></details>}
  </article>;
}

function freshness(brief: Brief): { label: string; level: 'good' | 'mid' | 'bad' } {
  const t = brief.sections.technicals;
  if (t.status === 'illustrative') return { label: 'Synthetic data', level: 'bad' };
  if (t.status === 'stale') return { label: `Price data stale${t.latest_bar ? ` · last bar ${t.latest_bar.slice(0, 10)}` : ''}`, level: 'bad' };
  if (t.status === 'unavailable' || t.status === 'insufficient_history') return { label: 'Price history unavailable', level: 'bad' };
  return { label: `Price data current${t.latest_bar ? ` · ${t.latest_bar.slice(0, 10)}` : ''}`, level: 'good' };
}

function AssessmentPanel({ brief, instrument, horizon, onHorizon }: {
  brief: Brief; instrument?: Instrument | null; horizon: HorizonKey; onHorizon: (h: HorizonKey) => void;
}) {
  const assessment: Assessment | undefined = brief.sections.assessment;
  const fresh = freshness(brief);
  const title = instrument ? `${instrument.display_name}` : 'Selected instrument';
  if (!assessment || assessment.error) {
    return <section className="rt-card rt-hero" aria-label="Primary assessment">
      <h2>{title}</h2>
      <div className="rt-reasons rt-reasons-block"><strong>No algorithmic assessment in this report</strong>
        <ul><li>{assessment?.error ?? 'This report was generated before the scoring engine was added. Run research again to calculate scores.'}</li></ul></div>
    </section>;
  }
  const h = assessment.horizons[horizon];
  const onKey = (event: KeyboardEvent<HTMLButtonElement>, index: number) => {
    if (event.key !== 'ArrowRight' && event.key !== 'ArrowLeft') return;
    event.preventDefault();
    const next = HORIZON_ORDER[(index + (event.key === 'ArrowRight' ? 1 : HORIZON_ORDER.length - 1)) % HORIZON_ORDER.length];
    onHorizon(next);
    document.getElementById(`rt-horizon-${next}`)?.focus();
  };
  return <section className="rt-card rt-hero" aria-label="Primary assessment">
    <header className="rt-hero-head">
      <div>
        <h2>{title}</h2>
        <p>{instrument ? `${instrument.symbol} · ${instrument.venue}` : ''} · {HORIZON_LABEL[horizon]} horizon</p>
      </div>
      <div className="rt-chips">
        <span className={`rt-chip rt-${fresh.level}`}>{fresh.label}</span>
        <span className="rt-chip">Assessed {ist(assessment.generated_at)}</span>
      </div>
    </header>
    <div className="rt-horizon-tabs" role="tablist" aria-label="Investment horizon">
      {HORIZON_ORDER.map((key, index) => {
        const item = assessment.horizons[key];
        return <button key={key} id={`rt-horizon-${key}`} role="tab" type="button" aria-selected={horizon === key} tabIndex={horizon === key ? 0 : -1}
          className={horizon === key ? 'active' : ''} onClick={() => onHorizon(key)} onKeyDown={(e) => onKey(e, index)}>
          <small>{HORIZON_LABEL[key]}</small>
          <b className={`rt-${tone(item.score)}`}>{item.score === null ? '—' : num(item.score, 0)}</b>
          <span>{REC_TEXT[item.recommendation]}</span>
        </button>;
      })}
    </div>
    <div className="rt-hero-body" role="tabpanel">
      <div className="rt-hero-score">
        <small>Algorithmic score · {HORIZON_LABEL[horizon]}</small>
        <div className="rt-score-row"><strong className={`rt-score rt-big rt-${tone(h.score)}`}>{h.score === null ? '—' : num(h.score, 1)}</strong><span>{h.score === null ? 'not calculated' : '/100'}</span></div>
        <ScoreBar score={h.score} />
        <div className="rt-rec"><span className={`rt-rec-chip rec-${h.recommendation}`}>{REC_TEXT[h.recommendation]}</span>
          {h.raw_recommendation && h.raw_recommendation !== h.recommendation && <small>Score alone indicated {REC_TEXT[h.raw_recommendation] ?? h.raw_recommendation}; a risk gate withheld it.</small>}</div>
        <dl className="rt-meta">
          <div><dt>Confidence</dt><dd>{h.confidence === null ? '—' : `${num(h.confidence, 0)}%`}</dd></div>
          <div><dt>Evidence quality</dt><dd>{num(h.scored_weight * 100, 0)}% of weight scored</dd></div>
        </dl>
      </div>
      <div className="rt-hero-why">
        <h3>How {HORIZON_LABEL[horizon]} is weighted</h3>
        <div className="rt-table-wrap"><table className="rt-table">
          <thead><tr><th>Dimension</th><th>Weight</th><th>Used</th><th>Score</th></tr></thead>
          <tbody>{presentKeys(assessment).map((key) => {
            const c = assessment.components[key];
            const used = h.contributions.find((x) => x.component === key);
            return <tr key={key}><td>{c.label.replace(' Intelligence', '')}</td><td>{h.weights[key] === undefined ? '—' : `${Math.round(h.weights[key] * 100)}%`}</td>
              <td>{used ? `${Math.round(used.effective_weight * 100)}%` : 'excluded'}</td><td>{c.score === null ? 'blocked' : num(c.score, 1)}</td></tr>;
          })}</tbody>
        </table></div>
        <Reasons title="Not assessed because" items={h.blockers} kind="block" />
        <Reasons title="Risk gates applied" items={h.gates.map((g) => g.detail)} kind="warn" />
      </div>
    </div>
    <p className="rt-provisional"><b>Provisional model</b> ({assessment.model_version}). {assessment.validation.statement} Not backtested. Decision support, not investment advice.</p>
    <div className="rt-component-grid">{presentKeys(assessment).map((key) => <ComponentCard key={key} component={assessment.components[key]} weight={h.weights[key]} />)}</div>
  </section>;
}

function FactorTable({ component }: { component: ScoreComponent }) {
  if (!component.factors.length) return <p className="rt-empty">No metrics could be computed. {component.blockers.join(' ')}</p>;
  return <div className="rt-table-wrap"><table className="rt-table">
    <thead><tr><th>Metric</th><th>Value</th><th>Sub-score</th><th>Basis</th></tr></thead>
    <tbody>{component.factors.map((f) => <tr key={f.key}><td>{f.label}</td><td>{num(f.value, 2)} {f.unit.startsWith('(') || f.unit.startsWith('of') ? '' : f.unit}</td>
      <td><ScoreBar score={f.sub_score} /></td><td>{f.basis}</td></tr>)}</tbody>
  </table></div>;
}

// Third item: true for monetary totals shown in compact units; EPS, margins and share counts keep their own units.
const STATEMENT_ROWS: [string, string, boolean][] = [
  ['revenue', 'Revenue', true], ['net_income', 'Net income', true], ['eps', 'EPS (per share)', false], ['operating_margin', 'Operating margin (%)', false],
  ['total_assets', 'Total assets', true], ['total_liabilities', 'Total liabilities', true], ['total_equity', 'Total equity', true],
  ['operating_cash_flow', 'Operating cash flow', true], ['free_cash_flow', 'Free cash flow', true], ['total_debt', 'Total debt', true], ['shares_outstanding', 'Shares outstanding (count)', false],
];

function StatementsTable({ brief }: { brief: Brief }) {
  const [kind, setKind] = useState<'annual' | 'quarterly'>('annual');
  const periods: FundamentalPeriod[] = brief.sections.fundamentals.periods
    .filter((p) => p.period_type === kind).sort((a, b) => b.period_end.localeCompare(a.period_end)).slice(0, 6);
  const trend = new Map(brief.sections.financials_trend.periods.filter((p) => p.period_type === kind).map((p) => [p.period_end, p]));
  const currencies = [...new Set(periods.map((p) => p.currency))];
  return <div>
    <div className="rt-toolbar">
      <div className="rt-seg" role="group" aria-label="Statement frequency">
        {(['annual', 'quarterly'] as const).map((k) => <button key={k} type="button" aria-pressed={kind === k} className={kind === k ? 'active' : ''} onClick={() => setKind(k)}>{k === 'annual' ? 'Annual' : 'Quarterly'}</button>)}
      </div>
      <small>Currency from provider metadata: {currencies.join(', ') || '—'}. Monetary totals are in compact Indian units (K, L, Cr, Lakh Cr) only when the currency is INR; other currencies are shown in full. Stored values are unchanged (hover a cell to see the raw value). “—” means not stored.</small>
    </div>
    {periods.length === 0 ? <p className="rt-empty">No {kind} periods are stored for this instrument.</p> : <div className="rt-table-wrap"><table className="rt-table rt-fin">
      <thead><tr><th>Line item</th>{periods.map((p) => <th key={p.period_end}>{p.period_end}{p.stale && <span className="rt-stale"> stale</span>}</th>)}</tr></thead>
      <tbody>
        {STATEMENT_ROWS.map(([key, label, money]) => <tr key={key}><th scope="row">{label}</th>{periods.map((p) => {
          const raw = p.values[key]?.value ?? null;
          if (key === 'shares_outstanding' && raw !== null && Number(raw) === 0) return <td key={p.period_end}>—</td>;
          return <td key={p.period_end} title={raw === null ? undefined : `Stored value: ${raw}${p.values[key]?.source ? ` · filled from ${p.values[key]?.source}${p.values[key]?.basis ? ` (${p.values[key]?.basis})` : ''}${p.values[key]?.derived ? ', derived' : ''}` : ''}`}>{money ? formatMoney(raw, p.currency) : num(raw, 2)}</td>;
        })}</tr>)}
        <tr className="rt-subhead"><th colSpan={periods.length + 1}>Year-on-year growth</th></tr>
        {(['revenue', 'net_income', 'eps', 'free_cash_flow'] as const).map((key) => <tr key={`g-${key}`}><th scope="row">{words(key)}</th>{periods.map((p) => {
          const view = formatChange(trend.get(p.period_end)?.values[key], { money: key !== 'eps', currency: p.currency });
          return <td key={p.period_end} className={view.tone === 'pos' ? 'rt-pos' : view.tone === 'neg' ? 'rt-neg' : ''}>
            {view.text}{view.detail && <small className="rt-change-detail">{view.detail}</small>}</td>;
        })}</tr>)}
      </tbody>
    </table></div>}
  </div>;
}

type HistoryBars = { bars: { timestamp: string; open: string; high: string; low: string; close: string; volume: string; source?: string }[] };

function TechnicalChart({ instrument }: { instrument: Instrument }) {
  const [state, setState] = useState<{ status: 'loading' | 'ready' | 'error'; bars: HistoryBars['bars']; error?: string }>({ status: 'loading', bars: [] });
  useEffect(() => {
    let active = true;
    setState({ status: 'loading', bars: [] });
    const end = new Date();
    const start = new Date(end.getTime() - 420 * 86_400_000);
    const query = new URLSearchParams({ interval: '1d', start: start.toISOString(), end: end.toISOString(), limit: '300' });
    apiRequest<HistoryBars>(`/market-data/instruments/${instrument.id}/history?${query}`)
      .then((result) => { if (active) setState({ status: 'ready', bars: result.bars }); })
      .catch((error: Error) => { if (active) setState({ status: 'error', bars: [], error: error.message }); });
    return () => { active = false; };
  }, [instrument.id]);
  if (state.status === 'loading') return <div className="rt-skeleton" aria-busy="true">Loading stored daily bars…</div>;
  if (state.status === 'error') return <p className="rt-empty" role="alert">Chart unavailable: {state.error}</p>;
  if (!state.bars.length) return <p className="rt-empty">No stored daily bars to chart. Use “Refresh data”.</p>;
  return <div className="rt-chart"><CandleChart bars={state.bars} tickSize={instrument.tick_size ?? null} currency={instrument.currency ?? 'INR'}
    timezone={instrument.timezone ?? 'Asia/Kolkata'} interval="1d" sessionClose={sessionCloseForVenue(instrument.venue)} /></div>;
}

function Sparkline({ points }: { points: SentimentHistoryPoint[] }) {
  if (points.length < 3) return <p className="rt-empty">Not enough analysed history to show a trend.</p>;
  const data = points.slice(-60);
  const w = 600, h = 120;
  const x = (i: number) => (i / (data.length - 1)) * (w - 20) + 10;
  const y = (s: number) => h / 2 - (s * (h / 2 - 10));
  return <svg className="rt-spark" viewBox={`0 0 ${w} ${h}`} role="img" aria-label="Sentiment history, scale −1 to +1" preserveAspectRatio="none">
    <line x1="0" x2={w} y1={h / 2} y2={h / 2} className="rt-axis" />
    <polyline fill="none" strokeWidth="2" className="rt-line" points={data.map((p, i) => `${x(i)},${y(p.score)}`).join(' ')} />
  </svg>;
}

function EventRow({ event }: { event: SentimentEvent }) {
  const lead = event.items[0];
  const publishers = [...new Set(event.items.map((i) => i.publisher ?? i.source))];
  return <article className="rt-event">
    <header>
      <strong>{lead?.headline ?? 'Event'}</strong>
      <span className={`rt-pill rt-s-${event.sentiment}`}>{event.sentiment}</span>
    </header>
    <p className="rt-event-meta">{words(event.event_category)} · impact {event.impact} · event confidence {event.confidence_score}/100 · {publishers.length} publisher{publishers.length === 1 ? '' : 's'}, {event.items.length} article{event.items.length === 1 ? '' : 's'} grouped as one event{lead ? ` · ${ist(lead.published_at)}` : ''}</p>
    {event.contradictory_sources && <p className="rt-conflict">Publishers disagree on this event.</p>}
    <details><summary>Individual publisher readings</summary>
      <ul>{event.items.map((i) => <li key={i.news_item_id}><span className={`rt-pill rt-s-${i.sentiment}`}>{i.sentiment}</span> {i.publisher ?? i.source} — {i.headline}
        {i.url && <> · <a href={i.url} target="_blank" rel="noreferrer">source</a></>}
        {i.summary && <small>{plainText(i.summary)} (feed summary; full article {i.full_text_status === 'available' ? 'stored' : 'not stored'})</small>}</li>)}</ul>
    </details>
  </article>;
}


const AI_REC: Record<string, string> = { ...REC_TEXT, SELL: 'Sell-leaning' };

const AI_LIST_LIMIT = 3;
function AiList({ title, items, limit = AI_LIST_LIMIT }: { title: string; items: string[]; limit?: number }) {
  const [all, setAll] = useState(false);
  if (!items.length) return null;
  const shown = all ? items : items.slice(0, limit);
  return <div className="rt-ai-list"><h5>{title}</h5><ul>{shown.map((item) => <li key={item}>{item}</li>)}</ul>
    {items.length > limit && <button type="button" className="rt-toggle" aria-expanded={all} onClick={() => setAll((v) => !v)}>{all ? 'Show less' : `Show ${items.length - limit} more`}</button>}</div>;
}

const CLAMP_CHARS = 260;
function AiText({ text }: { text: string }) {
  const [open, setOpen] = useState(false);
  const long = text.length > CLAMP_CHARS;
  return <div><p className={`rt-note${long && !open ? ' rt-clamp' : ''}`}>{text}</p>
    {long && <button type="button" className="rt-toggle" aria-expanded={open} onClick={() => setOpen((v) => !v)}>{open ? 'Show less' : 'Show more'}</button>}</div>;
}

const AI_VALUATION_LABEL: Record<string, string> = { expensive: 'Expensive', fairly_valued: 'Fairly valued', potentially_undervalued: 'Potentially undervalued', insufficient_evidence: 'Insufficient evidence' };

function AiValuationBlock({ view }: { view: AiValuationView }) {
  return <div className="rt-ai-list rt-ai-valuation">
    <h5>Valuation view: {AI_VALUATION_LABEL[view.label] ?? view.label}</h5>
    <p>{view.summary}{view.effect_on_assessment ? ` ${view.effect_on_assessment}` : ''}</p>
    <AiList title="Supporting metrics" items={view.supporting_metrics} limit={2} />
    <AiList title="Provider vs calculated" items={view.provider_vs_calculated} limit={2} />
    <AiList title="Limitations" items={view.limitations} limit={2} />
  </div>;
}

export function AiAnalysisPanel({ brief, horizon }: { brief: Brief; horizon: HorizonKey }) {
  const [status, setStatus] = useState<AiStatus | null>(null);
  const [loadError, setLoadError] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [fresh, setFresh] = useState<Partial<Record<HorizonKey, AiAnalysis>>>({});
  useEffect(() => {
    let active = true;
    setStatus(null);
    setLoadError('');
    setFresh({});
    apiRequest<AiStatus>(`/research/instruments/${brief.instrument_id}/ai-analysis`)
      .then((value) => { if (active) setStatus(value); })
      .catch((reason: Error) => { if (active) setLoadError(reason.message); });
    return () => { active = false; };
  }, [brief.instrument_id, brief.run_id]);
  const analysis = fresh[horizon] ?? status?.analyses[horizon];
  const algorithmic = brief.sections.assessment?.horizons?.[horizon];
  async function run() {
    setBusy(true);
    setError('');
    try {
      const result = await apiRequest<AiAnalysis>(`/research/instruments/${brief.instrument_id}/ai-analysis`, {
        method: 'POST', body: JSON.stringify({ horizon }),
      });
      setFresh((current) => ({ ...current, [horizon]: result }));
    } catch (reason) {
      setError(reason instanceof ApiRequestError || reason instanceof Error ? reason.message : 'AI analysis failed.');
    } finally {
      setBusy(false);
    }
  }
  const unavailable = status && (!status.enabled || !status.configured)
    ? (!status.enabled ? 'AI analysis is disabled on this server (AI_ENABLED).' : 'No Gemini API key (GEMINI_API_KEY) or valid GEMINI_MODEL is configured on the server.')
    : '';
  const cmp = analysis?.comparison;
  return <section className="rt-card rt-ai" aria-label="AI analyst">
    <header className="rt-ai-head">
      <div><h3>AI analyst · {HORIZON_LABEL[horizon]}</h3>
        <p className="rt-note">Optional and independent of the algorithmic score. Reads this stored snapshot only, may disagree with the algorithm, and never changes its score.</p></div>
      <button className="rt-btn rt-primary" type="button" onClick={() => void run()} disabled={busy || !!unavailable || !status || !!analysis}
        title={analysis ? 'An analysis already exists for this snapshot, model and horizon.' : undefined}>
        {busy ? 'Analysing…' : analysis ? 'Analysis stored' : 'Run AI Analysis'}
      </button>
    </header>
    {loadError && <p className="rt-alert rt-alert-error" role="alert">Could not load AI status: {loadError}</p>}
    {unavailable && <p className="rt-note" role="status">{unavailable} The algorithmic assessment above is unaffected.</p>}
    {error && <p className="rt-alert rt-alert-error" role="alert">{error}</p>}
    {busy && <p className="rt-note" aria-live="polite">Sending the stored snapshot to the AI provider…</p>}
    {!analysis && !busy && !unavailable && status && <p className="rt-note">No AI analysis yet for {HORIZON_LABEL[horizon]} on this snapshot. Running it makes one Gemini ({status.model}) API call; results are cached.</p>}
    {analysis && <div className="rt-ai-body">
      <div className="rt-ai-scores">
        <div className="rt-ai-score"><small>AI score</small>
          <strong className={`rt-score rt-${tone(analysis.result.score)}`}>{analysis.result.score === null ? '—' : num(analysis.result.score, 0)}</strong>
          <span className={`rt-rec-chip rec-${analysis.result.recommendation}`}>{AI_REC[analysis.result.recommendation] ?? analysis.result.recommendation}</span>
          <small>AI confidence {analysis.result.confidence}%</small></div>
        <div className="rt-ai-score"><small>Algorithmic score</small>
          <strong className={`rt-score rt-${tone(algorithmic?.score ?? null)}`}>{algorithmic?.score == null ? '—' : num(algorithmic.score, 1)}</strong>
          <span className={`rt-rec-chip rec-${algorithmic?.recommendation ?? 'INSUFFICIENT_EVIDENCE'}`}>{algorithmic ? REC_TEXT[algorithmic.recommendation] : 'Not available'}</span>
          <small>Unchanged by the AI</small></div>
      </div>
      {cmp && <div className={`rt-reasons rt-reasons-${cmp.level === 'material' ? 'warn' : 'info'}`} role="note">
        <strong>{cmp.level === 'not_comparable' ? 'Not comparable' : cmp.level === 'aligned' ? 'Broadly aligned' : cmp.level === 'moderate' ? 'Moderate disagreement' : 'Material disagreement'}</strong>
        <ul>
          {cmp.score_gap !== null && <li>AI score is {Math.abs(cmp.score_gap)} points {cmp.score_gap >= 0 ? 'above' : 'below'} the algorithmic score{cmp.recommendation_differs ? '; the recommendations also differ' : ''}.</li>}
          {cmp.note && <li>{cmp.note}</li>}
          {analysis.result.disagreement_explanation && <li>{analysis.result.disagreement_explanation}</li>}
        </ul></div>}
      <div><p className="rt-ai-summary"><strong>{analysis.result.summary}</strong></p><AiText text={analysis.result.reasoning} /></div>
      {(analysis.result.data_gaps.length > 0 || analysis.result.guardrails_applied.length > 0) && <div className="rt-ai-notes">
        <Reasons title="Missing, stale or thin data the AI acknowledged" items={analysis.result.data_gaps} kind="info" />
        <Reasons title="Guardrails applied" items={analysis.result.guardrails_applied} kind="warn" />
      </div>}
      <div className="rt-ai-grid">
        <AiList title="Strengths" items={analysis.result.strengths} />
        <AiList title="Weaknesses" items={analysis.result.weaknesses} />
        <AiList title="Risks" items={analysis.result.risks} />
      </div>
      <div className="rt-ai-grid">
        {([['Bull case', analysis.result.bull_case], ['Bear case', analysis.result.bear_case]] as const).map(([label, scenario]) => <div className="rt-ai-list" key={label}>
          <h5>{label}</h5><p>{scenario.thesis}</p>
          <AiList title="Drivers" items={scenario.drivers} limit={2} /><AiList title="Would be invalidated by" items={scenario.invalidated_by} limit={2} />
        </div>)}
        {analysis.result.valuation_view && <AiValuationBlock view={analysis.result.valuation_view} />}
      </div>
      <p className="rt-ai-foot">{analysis.source === 'local_gate' ? 'No AI call was made: the snapshot had no usable evidence. ' : ''}
        Gemini {analysis.model} · {analysis.prompt_version} · {ist(analysis.created_at)} · {HORIZON_LABEL[analysis.horizon]} · snapshot {analysis.snapshot_hash.slice(0, 10)}
        {analysis.cached ? ' · stored result (no new API call)' : ''} · {analysis.disclaimer}</p>
    </div>}
  </section>;
}

const PEER_LABEL: Record<string, string> = { pe_ttm: 'P/E', pb: 'P/B', price_to_sales: 'Price / Sales' };
const PRIMARY_KEYS = ['market_cap', 'enterprise_value', 'pe_ttm', 'pb', 'ev_ebitda', 'price_to_sales', 'peg', 'dividend_yield'];
const QUALITY_KEYS = ['roe', 'roce', 'interest_coverage', 'book_value_per_share'];
const PROVIDER_NAME: Record<string, string> = { dhan: 'Dhan', yfinance: 'Yahoo', screener: 'Screener', angel_one: 'Angel One', twelve_data: 'Twelve Data' };

const fixed2 = (value: string) => Number(value).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const hasValue = (m: ValuationMetric | undefined): m is ValuationMetric => !!m && m.value !== null;
const sourceLabel = (m: ValuationMetric) => m.origin === 'provider_reported' ? (PROVIDER_NAME[m.provider ?? ''] ?? m.provider ?? 'Provider') : m.origin === 'calculated' ? 'Calculated' : null;

function valuationText(metric: ValuationMetric, currency: string): string {
  if (metric.value === null) return 'Unavailable';
  if (metric.unit === '%') return `${fixed2(metric.value)}%`;
  if (metric.unit === 'x') return `${fixed2(metric.value)}x`;
  if (metric.unit.endsWith('/share')) return formatMoney(metric.value, currency);
  return formatMoney(metric.value, currency);
}

function ValuationCard({ metric, currency, compact = false }: { metric: ValuationMetric; currency: string; compact?: boolean }) {
  const source = sourceLabel(metric);
  const differs = metric.cross_check?.disagrees;
  return <div className={`rt-vcard${compact ? ' rt-vcard-compact' : ''}`}>
    <span className="rt-vlabel">{metric.label}</span>
    <b className="rt-vvalue">{valuationText(metric, currency)}</b>
    <span className="rt-vsource">{source}{differs ? <span className="rt-vflag" title={`The ${metric.cross_check?.origin} figure is ${metric.cross_check?.value}`}> · differs from calculated</span> : null}</span>
  </div>;
}

function PeerRow({ label, peer, group }: { label: string; peer: PeerMetric; group?: string }) {
  const parsed = peer.percentile_rank == null || peer.percentile_rank === '' ? NaN : Number(peer.percentile_rank);
  const rank = Number.isFinite(parsed) ? parsed : null; // missing rank is unknown, never 0 (= "cheapest")
  return <div className="rt-vpeer">
    <div className="rt-vpeer-head"><b>{label}</b><span>{peer.subject}x <small>vs peer median {peer.peer_median}x</small></span></div>
    {rank === null ? <small>{peer.peer_count} peers{group ? ` · ${group}` : ''} · percentile —</small> : <>
      <div className="rt-vpeer-bar" role="img" aria-label={`${label} is cheaper than ${100 - rank}% of peers`}>
        <span className="rt-vpeer-band" style={{ left: '25%', width: '50%' }} /><span className="rt-vpeer-dot" style={{ left: `${Math.min(97, Math.max(3, rank))}%` }} /></div>
      <small>{peer.peer_count} peers{group ? ` · ${group}` : ''} · {ordinal(rank)} percentile (lower is cheaper)</small></>}
  </div>;
}
const ordinal = (n: number) => `${Math.round(n)}${['th', 'st', 'nd', 'rd'][(Math.round(n) % 100 - 20) % 10] ?? ['th', 'st', 'nd', 'rd'][Math.round(n) % 100] ?? 'th'}`;

function ValuationPanel({ valuation }: { valuation: Valuation | undefined }) {
  if (!valuation || valuation.error) {
    return <p className="rt-empty">{valuation?.error ?? 'This report was generated before Valuation Intelligence existed. Run research again.'}</p>;
  }
  const all = Object.values(valuation.metrics);
  const primary = PRIMARY_KEYS.map((k) => valuation.metrics[k]).filter(hasValue);
  const quality = QUALITY_KEYS.map((k) => valuation.metrics[k]).filter(hasValue);
  const peers = valuation.peer_comparison;
  const compared = peers ? Object.entries(peers.metrics).filter(([, p]) => p.status === 'compared') : [];
  const thin = peers ? Object.entries(peers.metrics).filter(([, p]) => p.status !== 'compared') : [];
  const scoring = valuation.scoring;
  const shown = all.filter(hasValue);
  const missing = all.filter((m) => m.value === null);
  return <>
    <header className="rt-vhead">
      <h3>Valuation</h3>
      {valuation.price.current && <span className="rt-vprice">{formatMoney(valuation.price.value, valuation.currency)} <small>close {valuation.price.as_of}</small></span>}
      {scoring?.status === 'scored' && <span className="rt-pill rt-v-calculated">Score {num(scoring.score, 0)} · {scoring.label}</span>}
    </header>
    <p className="rt-vnote">Valuation contributes to Research scoring only where sufficient reliable evidence is available.</p>
    {primary.length === 0 && quality.length === 0 && <p className="rt-empty">No valuation metrics can be shown yet: {valuation.price.current ? 'the required fundamentals are missing.' : (valuation.price.problem ?? 'there is no current share price.')}</p>}
    {primary.length > 0 && <div className="rt-vgrid">{primary.map((m) => <ValuationCard key={m.key} metric={m} currency={valuation.currency} />)}</div>}
    {quality.length > 0 && <section className="rt-vsection"><h4>Financial quality</h4>
      <div className="rt-vgrid rt-vgrid-compact">{quality.map((m) => <ValuationCard key={m.key} metric={m} currency={valuation.currency} compact />)}</div></section>}
    {(compared.length > 0 || thin.length > 0) && <section className="rt-vsection"><h4>Peer comparison</h4>
      {compared.map(([key, p]) => <PeerRow key={key} label={PEER_LABEL[key] ?? key} peer={p} group={peers?.peer_group} />)}
      {compared.length === 0 && <p className="rt-vmuted">Not enough comparable peers yet ({Math.max(...thin.map(([, p]) => p.peer_count))} of {MIN_PEERS_SHOWN} needed).</p>}
      {compared.length > 0 && thin.length > 0 && <p className="rt-vmuted">{thin.map(([k]) => PEER_LABEL[k] ?? k).join(', ')}: not enough comparable peers.</p>}
    </section>}
    <details className="rt-more rt-vdetails"><summary>Sources, freshness and methodology</summary>
      <ul>
        {shown.map((m) => <li key={m.key}><b>{m.label}</b> — {sourceLabel(m) ?? 'n/a'}{m.reporting_period ? ` · period ${m.reporting_period}` : ''}{m.retrieved_at ? ` · retrieved ${m.retrieved_at.slice(0, 10)}` : ''}{m.statement_basis ? ` · ${m.statement_basis}` : ''}
          {m.basis ? <small>{m.basis}</small> : null}
          {m.cross_check ? <small>Cross-check ({m.cross_check.origin}): {m.cross_check.value}, {m.cross_check.difference_pct}% apart</small> : null}
          {m.warnings.map((w) => <small key={w}>Note: {w}</small>)}</li>)}
        {missing.length > 0 && <li><b>Not available</b>{missing.map((m) => <small key={m.key}>{m.label}: {m.reason ?? 'no data'}</small>)}</li>}
        <li><b>Scoring</b>{scoring ? <small>{scoring.version} · {scoring.status === 'scored' ? 'scored' : 'not scored'}</small> : null}
          {scoring?.status === 'blocked' && scoring.blockers.map((x) => <small key={x}>{x}</small>)}<small>{valuation.model_version} · Decision support, not investment advice.</small></li>
      </ul></details>
  </>;
}
const MIN_PEERS_SHOWN = 5;

function ShareholdingPanel({ shareholding }: { shareholding: Shareholding | undefined }) {
  if (!shareholding || shareholding.status === 'unavailable') {
    return <>
      <h3>Shareholding</h3>
      <p className="rt-empty">{shareholding?.warnings?.[0] ?? 'This report was generated before shareholding data was collected. Run research again.'}</p>
    </>;
  }
  return <>
    <h3>Shareholding</h3>
    <div className="rt-reasons rt-reasons-warn" role="note"><strong>Read with care</strong>
      <ul>
        <li>Reporting period: <b>{shareholding.reporting_period_note}</b>. These figures are an undated snapshot and must not be read as the latest quarter&apos;s shareholding pattern.</li>
        {shareholding.warnings.map((warning) => <li key={warning}>{warning}</li>)}
      </ul>
    </div>
    <div className="rt-table-wrap"><table className="rt-table">
      <thead><tr><th>Category</th><th>Holding</th><th>Source</th><th>Retrieved</th><th>Reporting period</th></tr></thead>
      <tbody>{shareholding.holdings.map((h) => <tr key={h.key}>
        <th scope="row">{h.label}</th>
        <td><b>{h.percent === null ? 'Not provided' : `${num(h.percent, 2)}%`}</b></td>
        <td>Dhan</td>
        <td>{ist(shareholding.retrieved_at)}</td>
        <td>Not provided by source</td>
      </tr>)}</tbody>
    </table></div>
    <p className="rt-note">Itemised above: {shareholding.sum_percent === null || shareholding.sum_percent === undefined ? '—' : `${num(shareholding.sum_percent, 2)}%`}. Not itemised by the source: {shareholding.not_itemised_percent === null || shareholding.not_itemised_percent === undefined ? '—' : `${num(shareholding.not_itemised_percent, 2)}%`} (other categories such as government, trusts or depositary receipts are not returned).</p>
  </>;
}

export function ResearchBriefView({ brief, instrument, horizon = 'positional', onHorizon, sentimentHistory = [], showAi = false }: {
  brief: Brief; instrument?: Instrument | null; horizon?: HorizonKey; onHorizon?: (h: HorizonKey) => void; sentimentHistory?: SentimentHistoryPoint[]; showAi?: boolean;
}) {
  const [localHorizon, setLocalHorizon] = useState<HorizonKey>(horizon);
  const active = onHorizon ? horizon : localHorizon;
  const setActive = onHorizon ?? setLocalHorizon;
  const [tab, setTab] = useState<Tab>('Overview');
  const { fundamentals, technicals, news, news_sentiment: sentiment, collector_status: collectors, data_quality: quality } = brief.sections;
  const assessment = brief.sections.assessment && !brief.sections.assessment.error ? brief.sections.assessment : undefined;
  const companyEvents = useMemo(() => (sentiment?.events ?? []).filter((e) => e.items.some((i) => i.instrument_id === brief.instrument_id)), [sentiment, brief.instrument_id]);
  const marketEvents = (sentiment?.events.length ?? 0) - companyEvents.length;
  const groupedQuality = useMemo(() => {
    const groups = new Map<string, QualityItem & { count: number }>();
    quality.items.forEach((item) => {
      const key = `${item.kind}|${item.reason}`;
      groups.set(key, { ...item, count: (groups.get(key)?.count ?? 0) + 1 });
    });
    return [...groups.values()];
  }, [quality.items]);
  const onTabKey = (event: KeyboardEvent<HTMLButtonElement>, index: number) => {
    if (event.key !== 'ArrowRight' && event.key !== 'ArrowLeft') return;
    event.preventDefault();
    const next = TABS[(index + (event.key === 'ArrowRight' ? 1 : TABS.length - 1)) % TABS.length];
    setTab(next);
    document.getElementById(`rt-tab-${next}`)?.focus();
  };
  const snap = fundamentals.snapshot.values ?? {};
  const allRisks = assessment ? presentKeys(assessment).flatMap((k) => assessment.components[k].risk_flags.map((f) => `${assessment.components[k].label}: ${f}`)) : [];

  return <div className="rt-report" id="research-report">
    <AssessmentPanel brief={brief} instrument={instrument} horizon={active} onHorizon={setActive} />
    {showAi && <AiAnalysisPanel brief={brief} horizon={active} />}
    <div className="rt-tabs" role="tablist" aria-label="Research sections">
      {TABS.map((name, index) => <button key={name} id={`rt-tab-${name}`} role="tab" type="button" aria-selected={tab === name} aria-controls="rt-panel"
        tabIndex={tab === name ? 0 : -1} className={tab === name ? 'active' : ''} onClick={() => setTab(name)} onKeyDown={(e) => onTabKey(e, index)}>{name}</button>)}
    </div>
    <section className="rt-card rt-panel" id="rt-panel" role="tabpanel" aria-labelledby={`rt-tab-${tab}`}>
      {tab === 'Overview' && <>
        <h3>What the system knows, calculates and recommends</h3>
        {assessment ? <div className="rt-overview-grid">
          {presentKeys(assessment).map((key) => {
            const c = assessment.components[key];
            return <div key={key} className="rt-ov-row"><span>{c.label.replace(' Intelligence', '')}</span>
              <b className={`rt-${tone(c.score)}`}>{c.score === null ? 'Not scored' : `${num(c.score, 1)} · ${c.interpretation}`}</b>
              {c.status === 'scored' ? <small>confidence {num(c.confidence, 0)}% · coverage {num(c.coverage_pct, 0)}%</small>
                : <details className="rt-ov-why"><summary>{c.blockers.length ? 'Insufficient evidence — details' : 'Not scored'}</summary>
                  {c.blockers.length > 0 && <ul>{c.blockers.map((b) => <li key={b}>{b}</li>)}</ul>}</details>}</div>;
          })}
        </div> : <p className="rt-empty">Scores are not part of this older report. Run research again.</p>}
        <h4>Company snapshot</h4>
        {fundamentals.snapshot.status === 'available' ? <dl className="rt-kv">{Object.entries(snap).map(([name, item]) => <div key={name}><dt>{words(name)}</dt><dd>{item.value === null ? '—' : name === 'market_cap' ? formatMoney(item.value, instrument?.currency) : Number.isFinite(Number(item.value)) && name !== 'sector' && name !== 'industry' ? num(item.value, 2) : item.value}</dd></div>)}</dl>
          : <p className="rt-empty">No company snapshot is stored.</p>}
        {fundamentals.snapshot.conflicts && fundamentals.snapshot.conflicts.length > 0 && <Reasons title="Providers disagree (values kept separate, never averaged)" kind="warn"
          items={fundamentals.snapshot.conflicts.map((c) => `${words(c.field)}: ${c.source_a} ${c.value_a} vs ${c.source_b} ${c.value_b}`)} />}
      </>}
      {tab === 'Fundamentals' && assessment && <>
        <h3>Fundamental Intelligence</h3>
        {assessment.financial_sector && <p className="rt-note">Financial-sector company: leverage, margin and cash-flow ratios are excluded as not comparable; asset quality and capital adequacy are not stored.</p>}
        <FactorTable component={assessment.components.fundamental} />
        <Reasons title="Blocked because" items={assessment.components.fundamental.blockers} kind="block" />
        <Reasons title="Missing evidence" items={assessment.components.fundamental.missing} kind="info" />
        <Reasons title="Risk flags" items={assessment.components.fundamental.risk_flags} kind="warn" />
        <p className="rt-note">{assessment.components.fundamental.notes.join(' ')}</p>
      </>}
      {tab === 'Fundamentals' && !assessment && <p className="rt-empty">No scoring in this older report. Run research again.</p>}
      {tab === 'Valuation' && <ValuationPanel valuation={brief.sections.valuation} />}
      {tab === 'Shareholding' && <ShareholdingPanel shareholding={brief.sections.shareholding} />}
      {tab === 'Financial Statements' && <>
        <h3>Financial statements</h3>
        <StatementsTable brief={brief} />
        {assessment && <><h4>Financial Trends Intelligence</h4><FactorTable component={assessment.components.financial_trends} />
          <Reasons title="Conflicting evidence" items={assessment.components.financial_trends.conflicts} kind="warn" />
          <Reasons title="Blocked because" items={assessment.components.financial_trends.blockers} kind="block" />
          <Reasons title="Missing evidence" items={assessment.components.financial_trends.missing} kind="info" /></>}
      </>}
      {tab === 'Technical Analysis' && <>
        <h3>Technical analysis · stored daily bars</h3>
        {technicals.staleness?.reason && <div className="rt-reasons rt-reasons-block"><strong>Technical data is not current</strong><ul><li>{technicals.staleness.reason}</li></ul></div>}
        {technicals.message && !technicals.staleness?.reason && <p className="rt-note">{technicals.message}</p>}
        <div className="rt-tech-layout">
          <div>{instrument && !instrument.is_synthetic ? <TechnicalChart instrument={instrument} /> : <p className="rt-empty">No chart for synthetic or unselected instruments.</p>}
            <p className="rt-note">Levels Giver output is not available: that module does not exist in this repository, so no levels are shown or invented.</p></div>
          <div className="rt-table-wrap"><table className="rt-table rt-compact">
            <thead><tr><th>Indicator</th><th>Value</th><th>Bars</th></tr></thead>
            <tbody>{Object.entries(technicals.indicators).map(([name, item]) => <tr key={name}><td>{words(name)}</td><td>{item.value === null ? 'Insufficient history' : num(item.value, 2)}</td><td>{item.bars_used}/{item.bars_required}</td></tr>)}</tbody>
          </table></div>
        </div>
        {assessment && <><h4>Technical Intelligence</h4><FactorTable component={assessment.components.technical} />
          <Reasons title="Blocked because" items={assessment.components.technical.blockers} kind="block" />
          <Reasons title="Risk flags" items={assessment.components.technical.risk_flags} kind="warn" /></>}
        <small className="rt-note">Source {technicals.source ?? '—'} · latest bar {technicals.latest_bar ? technicals.latest_bar.slice(0, 10) : '—'}{technicals.staleness?.expected_session ? ` · expected session ${technicals.staleness.expected_session}` : ''}</small>
      </>}
      {tab === 'News' && <>
        <h3>Company news</h3>
        <p className="rt-note">Stored headlines and permitted summaries only; full article text is not assumed. Duplicate articles on one event are grouped. Market-wide news is under the collapsed “Market-wide news” panel above.</p>
        {companyEvents.length === 0 ? <p className="rt-empty">No analysed company-mapped news events in the period. Missing news is not treated as neutral.</p> : companyEvents.map((e) => <EventRow key={e.event_key} event={e} />)}
        {marketEvents > 0 && <p className="rt-note">{marketEvents} market/sector/macro event group(s) were also in the sentiment window and are excluded from company evidence.</p>}
        {news.items.length > 0 && <><h4>Headlines from configured company feeds</h4><ul className="rt-list">{news.items.map((item, index) => <li key={`${item.source_id}-${index}`}>
          <a href={item.url} target="_blank" rel="noreferrer">{item.headline}</a><small>{item.publisher} · {item.published_at ? ist(item.published_at) : 'publication time unavailable'}</small></li>)}</ul></>}
        {news.items.length === 0 && news.message && <p className="rt-note">{news.message}</p>}
      </>}
      {tab === 'Sentiment' && <>
        <h3>News &amp; Sentiment Intelligence</h3>
        {assessment ? <>
          <dl className="rt-kv">
            <div><dt>Company events</dt><dd>{companyEvents.length}</dd></div>
            <div><dt>Market-wide events (context)</dt><dd>{marketEvents}</dd></div>
            <div><dt>Score</dt><dd>{assessment.components.news_sentiment.score === null ? 'Not scored' : num(assessment.components.news_sentiment.score, 1)}</dd></div>
            <div><dt>Confidence</dt><dd>{assessment.components.news_sentiment.confidence === null ? '—' : `${num(assessment.components.news_sentiment.confidence, 0)}%`}</dd></div>
          </dl>
          <Reasons title="Not scored because" items={assessment.components.news_sentiment.blockers} kind="block" />
          <Reasons title="Contradictory evidence" items={assessment.components.news_sentiment.conflicts} kind="warn" />
          <Reasons title="Flags" items={assessment.components.news_sentiment.risk_flags} kind="warn" />
          <p className="rt-note">{assessment.components.news_sentiment.notes.join(' ')}</p>
        </> : null}
        {sentiment && <p className="rt-note">Event-level sentiment groups duplicate coverage into one reading; individual publisher readings are listed inside each event. Classifier {sentiment.classifier_version ?? 'unknown'} · validation {sentiment.validation_status ?? 'unknown'} (heuristic, not predictive).</p>}
        <h4>Sentiment history (company)</h4><Sparkline points={sentimentHistory} />
        {companyEvents.slice(0, 8).map((e) => <EventRow key={e.event_key} event={e} />)}
      </>}
      {tab === 'Risks & Scenarios' && <>
        <h3>Risks &amp; scenarios</h3>
        {assessment ? <>
          <Reasons title="Active risk flags" items={allRisks} kind="warn" />
          {allRisks.length === 0 && <p className="rt-empty">No component risk flags were raised from available data.</p>}
          <Reasons title={`Gates applied · ${HORIZON_LABEL[active]}`} items={assessment.horizons[active].gates.map((g) => g.detail)} kind="warn" />
          <Reasons title="Risk gates not yet implemented (absence of a flag here is not an all-clear)" items={assessment.not_implemented_gates} kind="info" />
        </> : <p className="rt-empty">No scoring in this older report.</p>}
        <p className="rt-note">Scenario analysis (bull/base/bear) is not produced: there is no scenario engine, and no figures are invented.</p>
      </>}
      {tab === 'Data Quality / Sources' && <>
        <h3>Data quality &amp; sources</h3>
        {groupedQuality.length === 0 ? <p className="rt-empty">No stale, unavailable, failed-fetch or synthetic items recorded.</p> : <ul className="rt-list">{groupedQuality.map((item) => <li key={`${item.kind}-${item.reason}`}>
          <span className={`rt-pill rt-q-${item.kind}`}>{words(item.kind)}</span> {item.reason}{item.count > 1 && <small> · repeated across {item.count} fields</small>}</li>)}</ul>}
        {brief.failures.length > 0 && <Reasons title="Sources that failed" kind="block" items={brief.failures.map((f) => `${f.source} (${f.stage}): ${f.reason}`)} />}
        {collectors && <details className="rt-more"><summary>Collector status</summary><ul className="rt-list">{Object.entries(collectors).map(([name, item]) => <li key={name}><b>{words(name)}</b>: {item.state}{item.detail ? ` — ${item.detail}` : ''}</li>)}</ul></details>}
        <p className="rt-note">Run {brief.status} · started {ist(brief.started_at)}{brief.finished_at ? ` · finished ${ist(brief.finished_at)}` : ''} · {brief.sources_succeeded}/{brief.sources_attempted} permitted sources succeeded.</p>
      </>}
    </section>
    <p className="rt-disclaimer">{brief.sections.disclaimer} Decision support, not investment advice.</p>
  </div>;
}
