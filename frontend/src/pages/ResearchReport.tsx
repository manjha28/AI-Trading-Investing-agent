import { useEffect, useMemo, useState, type KeyboardEvent } from 'react';
import { apiRequest } from '../api';
import { CandleChart, sessionCloseForVenue } from '../components/CandleChart';
import type {
  Assessment, Brief, ComponentKey, FundamentalPeriod, HorizonKey, Instrument, QualityItem, ScoreComponent,
  SentimentEvent, SentimentHistoryPoint,
} from './researchTypes';

const HORIZON_ORDER: HorizonKey[] = ['intraday', 'swing', 'positional', 'long_term'];
const HORIZON_LABEL: Record<HorizonKey, string> = { intraday: 'Intraday', swing: 'Swing', positional: 'Positional', long_term: 'Long-term' };
const COMPONENT_ORDER: ComponentKey[] = ['fundamental', 'financial_trends', 'technical', 'news_sentiment'];
const TABS = ['Overview', 'Fundamentals', 'Financial Statements', 'Technical Analysis', 'News', 'Sentiment', 'Risks & Scenarios', 'Data Quality / Sources'] as const;
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
    <span style={{ width: `${score ?? 0}%` }} />
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
    <Reasons title="Blocked because" items={component.blockers} kind="block" />
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
          <tbody>{COMPONENT_ORDER.map((key) => {
            const c = assessment.components[key];
            const used = h.contributions.find((x) => x.component === key);
            return <tr key={key}><td>{c.label.replace(' Intelligence', '')}</td><td>{Math.round(h.weights[key] * 100)}%</td>
              <td>{used ? `${Math.round(used.effective_weight * 100)}%` : 'excluded'}</td><td>{c.score === null ? 'blocked' : num(c.score, 1)}</td></tr>;
          })}</tbody>
        </table></div>
        <Reasons title="Not assessed because" items={h.blockers} kind="block" />
        <Reasons title="Risk gates applied" items={h.gates.map((g) => g.detail)} kind="warn" />
      </div>
    </div>
    <p className="rt-provisional"><b>Provisional model</b> ({assessment.model_version}). {assessment.validation.statement} Not backtested. Decision support, not investment advice.</p>
    <div className="rt-component-grid">{COMPONENT_ORDER.map((key) => <ComponentCard key={key} component={assessment.components[key]} weight={h.weights[key]} />)}</div>
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

const STATEMENT_ROWS: [string, string][] = [
  ['revenue', 'Revenue'], ['net_income', 'Net income'], ['eps', 'EPS'], ['operating_margin', 'Operating margin (%)'],
  ['total_assets', 'Total assets'], ['total_liabilities', 'Total liabilities'], ['total_equity', 'Total equity'],
  ['operating_cash_flow', 'Operating cash flow'], ['free_cash_flow', 'Free cash flow'], ['total_debt', 'Total debt'], ['shares_outstanding', 'Shares outstanding'],
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
      <small>Currency as reported by provider: {currencies.join(', ') || '—'}. Unit scale is not specified by the provider and is shown as stored. “—” means not stored.</small>
    </div>
    {periods.length === 0 ? <p className="rt-empty">No {kind} periods are stored for this instrument.</p> : <div className="rt-table-wrap"><table className="rt-table rt-fin">
      <thead><tr><th>Line item</th>{periods.map((p) => <th key={p.period_end}>{p.period_end}{p.stale && <span className="rt-stale"> stale</span>}</th>)}</tr></thead>
      <tbody>
        {STATEMENT_ROWS.map(([key, label]) => <tr key={key}><th scope="row">{label}</th>{periods.map((p) => {
          const raw = p.values[key]?.value ?? null;
          return <td key={p.period_end}>{key === 'shares_outstanding' && raw !== null && Number(raw) === 0 ? '—' : num(raw, 2)}</td>;
        })}</tr>)}
        <tr className="rt-subhead"><th colSpan={periods.length + 1}>Year-on-year growth</th></tr>
        {(['revenue', 'net_income', 'eps', 'free_cash_flow'] as const).map((key) => <tr key={`g-${key}`}><th scope="row">{words(key)}</th>{periods.map((p) => {
          const g = trend.get(p.period_end)?.values[key]?.year_on_year_growth_pct ?? null;
          return <td key={p.period_end} className={g !== null ? (Number(g) >= 0 ? 'rt-pos' : 'rt-neg') : ''}>{g === null ? '—' : `${num(g, 1)}%`}</td>;
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

export function ResearchBriefView({ brief, instrument, horizon = 'positional', onHorizon, sentimentHistory = [] }: {
  brief: Brief; instrument?: Instrument | null; horizon?: HorizonKey; onHorizon?: (h: HorizonKey) => void; sentimentHistory?: SentimentHistoryPoint[];
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
  const allRisks = assessment ? COMPONENT_ORDER.flatMap((k) => assessment.components[k].risk_flags.map((f) => `${assessment.components[k].label}: ${f}`)) : [];

  return <div className="rt-report" id="research-report">
    <AssessmentPanel brief={brief} instrument={instrument} horizon={active} onHorizon={setActive} />
    <div className="rt-tabs" role="tablist" aria-label="Research sections">
      {TABS.map((name, index) => <button key={name} id={`rt-tab-${name}`} role="tab" type="button" aria-selected={tab === name} aria-controls="rt-panel"
        tabIndex={tab === name ? 0 : -1} className={tab === name ? 'active' : ''} onClick={() => setTab(name)} onKeyDown={(e) => onTabKey(e, index)}>{name}</button>)}
    </div>
    <section className="rt-card rt-panel" id="rt-panel" role="tabpanel" aria-labelledby={`rt-tab-${tab}`}>
      {tab === 'Overview' && <>
        <h3>What the system knows, calculates and recommends</h3>
        {assessment ? <div className="rt-overview-grid">
          {COMPONENT_ORDER.map((key) => {
            const c = assessment.components[key];
            return <div key={key} className="rt-ov-row"><span>{c.label}</span>
              <b className={`rt-${tone(c.score)}`}>{c.score === null ? 'Not scored' : `${num(c.score, 1)} · ${c.interpretation}`}</b>
              <small>{c.status === 'scored' ? `confidence ${num(c.confidence, 0)}% · coverage ${num(c.coverage_pct, 0)}%` : c.blockers[0]}</small></div>;
          })}
        </div> : <p className="rt-empty">Scores are not part of this older report. Run research again.</p>}
        <h4>Company snapshot</h4>
        {fundamentals.snapshot.status === 'available' ? <dl className="rt-kv">{Object.entries(snap).map(([name, item]) => <div key={name}><dt>{words(name)}</dt><dd>{item.value === null ? '—' : Number.isFinite(Number(item.value)) && name !== 'sector' && name !== 'industry' ? num(item.value, 2) : item.value}</dd></div>)}</dl>
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
