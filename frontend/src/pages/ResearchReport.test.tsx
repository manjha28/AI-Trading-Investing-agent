import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import type { Assessment, Brief, ComponentKey, HorizonAssessment, HorizonKey, ScoreComponent } from './researchTypes';

vi.mock('../components/CandleChart', () => ({ CandleChart: () => null, sessionCloseForVenue: () => null }));
vi.mock('../api', () => ({ apiRequest: vi.fn(() => new Promise(() => undefined)), ApiRequestError: class extends Error {} }));

import { apiRequest } from '../api';
import { AiAnalysisPanel, ResearchBriefView, plainText } from './ResearchReport';
import { waitFor } from '@testing-library/react';

const component = (key: ComponentKey, over: Partial<ScoreComponent> = {}): ScoreComponent => ({
  key, label: `${key} Intelligence`, status: 'scored', score: 72, interpretation: 'strong', confidence: 66, coverage_pct: 80,
  factors: [{ key: 'f', label: 'Return on equity', value: 18.2, unit: '%', sub_score: 78, basis: 'b' }],
  missing: [], conflicts: [], blockers: [], risk_flags: [], notes: [], ...over,
});
const horizon = (key: HorizonKey, over: Partial<HorizonAssessment> = {}): HorizonAssessment => ({
  horizon: key, label: key, weights: { fundamental: 0.25, technical: 0.25, financial_trends: 0.3, news_sentiment: 0.2 } as HorizonAssessment['weights'], scored_weight: 1,
  blockers: [], score: 70, recommendation: 'BUY', raw_recommendation: 'BUY', confidence: 61, components_used: [], components_missing: [],
  gates: [], contributions: [], ...over,
});
function brief(assessment?: Assessment): Brief {
  return {
    run_id: 'r', instrument_id: 'i1', status: 'complete', started_at: '2026-11-05T04:00:00Z', finished_at: '2026-11-05T04:01:00Z',
    sources_attempted: 0, sources_succeeded: 0, failures: [], renderer: 'template', created_at: '2026-11-05T04:01:00Z', data_quality: [],
    sections: {
      fundamentals: { status: 'available', snapshot: { status: 'unavailable', values: null }, periods: [] },
      technicals: { status: 'available', message: null, interval: '1d', source: 'dhan', latest_bar: '2026-11-04T00:00:00Z', bars_available: 250, indicators: {} },
      financials_trend: { status: 'unavailable', periods: [] },
      news: { status: 'unavailable', message: null, items: [], sources: [] },
      data_quality: { status: 'clear', items: [] }, disclaimer: 'Decision support, not investment advice.', assessment,
    },
  } as Brief;
}
const assessment = (over: Partial<Assessment> = {}): Assessment => ({
  model_version: 'research-score-0.1.0-provisional', generated_at: '2026-11-05T04:01:00Z', default_horizon: 'positional',
  validation: { status: 'provisional', backtested: false, statement: 'Weights are unvalidated hypotheses.' },
  weights: {} as Assessment['weights'], thresholds: {}, sector: null, financial_sector: false, not_implemented_gates: ['liquidity'],
  components: {
    fundamental: component('fundamental'), financial_trends: component('financial_trends'),
    technical: component('technical', { status: 'blocked', score: null, interpretation: null, confidence: null, blockers: ['Latest bar 2026-11-03; expected 2026-11-04.'] }),
    news_sentiment: component('news_sentiment', { status: 'blocked', score: null, interpretation: null, confidence: null, blockers: ['Only 1 company-specific event(s).'] }),
  } as Assessment['components'], // a model-0.1.0 assessment: no valuation component

  horizons: {
    swing: horizon('swing', { score: null, recommendation: 'INSUFFICIENT_EVIDENCE', raw_recommendation: null, blockers: ['Technical Intelligence is required for Swing and is blocked.'] }),
    positional: horizon('positional', { score: 71.4, recommendation: 'WAIT', raw_recommendation: 'BUY', gates: [{ code: 'price_not_current', detail: 'Technical data is stale.' }] }),
    long_term: horizon('long_term', { score: 66, recommendation: 'HOLD', raw_recommendation: 'HOLD' }),
  },
  ...over,
});

afterEach(cleanup);

describe('plainText', () => {
  it('strips feed markup instead of rendering raw HTML', () => {
    expect(plainText('<p>Profit <b>up</b> 5%</p><script>x()</script><a href="u">link</a>')).toBe('Profit up 5%link');
    expect(plainText(null)).toBe('');
  });
});

describe('ResearchBriefView', () => {
  it('shows the calculated score, withheld recommendation and the gate that caused it', () => {
    render(<ResearchBriefView brief={brief(assessment())} instrument={{ id: 'i1', symbol: 'RELIANCE', display_name: 'Reliance Industries', venue: 'NSE', is_synthetic: false }} />);
    const hero = screen.getByRole('region', { name: 'Primary assessment' });
    expect(within(hero).getByText('71.4')).toBeInTheDocument();
    expect(within(hero).getAllByText('Wait').length).toBeGreaterThan(0);
    expect(within(hero).getByText(/a risk gate withheld it/)).toBeInTheDocument();
    expect(within(hero).getByText('Technical data is stale.')).toBeInTheDocument();
    expect(within(hero).getByText(/Provisional model/)).toBeInTheDocument();
  });

  it('shows the precise blocker for a blocked horizon and for blocked components, never a placeholder score', () => {
    render(<ResearchBriefView brief={brief(assessment())} instrument={null} />);
    fireEvent.click(screen.getByRole('tab', { name: /Swing/ }));
    expect(screen.getByText('Technical Intelligence is required for Swing and is blocked.')).toBeInTheDocument();
    expect(screen.getAllByText('Latest bar 2026-11-03; expected 2026-11-04.').length).toBeGreaterThan(0);
    expect(within(screen.getByLabelText('technical Intelligence')).getByText('Not scored')).toBeInTheDocument();
    expect(within(screen.getByLabelText('technical Intelligence')).queryByText(/\d+\.\d/)).toBeNull();
  });

  it('switches the primary assessment when a horizon is selected', () => {
    render(<ResearchBriefView brief={brief(assessment())} instrument={null} />);
    fireEvent.click(screen.getByRole('tab', { name: /Long-term/ }));
    expect(within(screen.getByRole('region', { name: 'Primary assessment' })).getAllByText('66').length).toBeGreaterThan(0);
    expect(screen.getByText(/How Long-term is weighted/)).toBeInTheDocument();
  });

  it('explains a legacy report without an assessment instead of inventing scores', () => {
    render(<ResearchBriefView brief={brief(undefined)} instrument={null} />);
    expect(screen.getByText(/generated before the scoring engine was added/)).toBeInTheDocument();
  });

  it('surfaces a scoring error without hiding the evidence tabs', () => {
    render(<ResearchBriefView brief={brief(assessment({ error: 'Scoring failed (ValueError); evidence is unaffected.' }))} instrument={null} />);
    expect(screen.getByText(/Scoring failed \(ValueError\)/)).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Data Quality / Sources' })).toBeInTheDocument();
  });

  it('shows the stale-technical reason on the technical tab and states Levels Giver is unavailable', () => {
    const b = brief(assessment());
    b.sections.technicals = { ...b.sections.technicals, status: 'stale', staleness: { stale: true, basis: 'nse_session_calendar', latest_session: '2026-11-03', expected_session: '2026-11-04', reason: 'Latest stored daily bar is 2026-11-03; the last completed NSE session is 2026-11-04.' } };
    render(<ResearchBriefView brief={b} instrument={null} />);
    fireEvent.click(screen.getByRole('tab', { name: 'Technical Analysis' }));
    expect(screen.getByText(/last completed NSE session is 2026-11-04/)).toBeInTheDocument();
    expect(screen.getByText(/Levels Giver output is not available/)).toBeInTheDocument();
  });

  it('renders empty states for news and financial statements', () => {
    render(<ResearchBriefView brief={brief(assessment())} instrument={null} />);
    fireEvent.click(screen.getByRole('tab', { name: 'News' }));
    expect(screen.getByText(/Missing news is not treated as neutral/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('tab', { name: 'Financial Statements' }));
    expect(screen.getByText(/No annual periods are stored/)).toBeInTheDocument();
  });
});


describe('AiAnalysisPanel', () => {
  const mocked = vi.mocked(apiRequest);
  const aiResult = (over: Record<string, unknown> = {}) => ({
    id: 'a1', brief_id: 'b', horizon: 'positional', model: 'test-model', prompt_version: 'ai-analyst-v1', snapshot_hash: 'abcdef1234567890',
    provider: 'gemini', source: 'gemini', created_at: '2026-11-05T05:00:00Z', cached: false, disclaimer: 'AI disclaimer', usage: null,
    comparison: { algorithmic_score: 71.4, algorithmic_recommendation: 'WAIT', ai_score: 40, ai_recommendation: 'AVOID', score_gap: -31.4, level: 'material', recommendation_differs: true },
    result: {
      score: 40, recommendation: 'AVOID', confidence: 55, summary: 'Weak balance sheet evidence.', reasoning: 'Reasoning text.',
      strengths: ['Low debt'], weaknesses: ['Thin cash flow'], risks: ['Sector risk'],
      bull_case: { thesis: 'Bull thesis', drivers: ['Growth'], invalidated_by: ['Margin squeeze'] },
      bear_case: { thesis: 'Bear thesis', drivers: ['Valuation'], invalidated_by: ['Re-rating'] },
      data_gaps: ['Technical data is stale.'], disagreement_explanation: 'The AI weights cash flow more heavily.', guardrails_applied: [],
    }, ...over,
  });
  const status = (over: Record<string, unknown> = {}) => ({ enabled: true, configured: true, provider: 'gemini', model: 'test-model', brief_id: 'b', analyses: {}, ...over });
  const assessed = () => brief(assessment());

  afterEach(() => mocked.mockReset());

  it('is disabled with a clear reason when the server has AI off, leaving the algorithmic report alone', async () => {
    mocked.mockResolvedValueOnce(status({ enabled: false }));
    render(<AiAnalysisPanel brief={assessed()} horizon="positional" />);
    expect(await screen.findByText(/AI analysis is disabled on this server/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Run AI Analysis/ })).toBeDisabled();
  });

  it('shows the missing-key reason and never offers to run', async () => {
    mocked.mockResolvedValueOnce(status({ configured: false }));
    render(<AiAnalysisPanel brief={assessed()} horizon="positional" />);
    expect(await screen.findByText(/No Gemini API key \(GEMINI_API_KEY\) or valid GEMINI_MODEL is configured on the server/)).toBeInTheDocument();
  });

  it('runs on click, then shows AI and algorithmic scores separately with the disagreement explained', async () => {
    mocked.mockResolvedValueOnce(status()).mockResolvedValueOnce(aiResult());
    render(<AiAnalysisPanel brief={assessed()} horizon="positional" />);
    const button = await screen.findByRole('button', { name: 'Run AI Analysis' });
    await waitFor(() => expect(button).toBeEnabled());
    expect(mocked).toHaveBeenCalledTimes(1); // nothing is sent to the AI until the user clicks
    fireEvent.click(button);
    expect(await screen.findByText('Weak balance sheet evidence.')).toBeInTheDocument();
    expect(screen.getByText(/Gemini test-model/)).toBeInTheDocument();
    expect(mocked).toHaveBeenLastCalledWith('/research/instruments/i1/ai-analysis', expect.objectContaining({ method: 'POST', body: JSON.stringify({ horizon: 'positional' }) }));
    const panel = screen.getByRole('region', { name: 'AI analyst' });
    expect(within(panel).getByText('AI score')).toBeInTheDocument();
    expect(within(panel).getByText('Algorithmic score')).toBeInTheDocument();
    expect(within(panel).getByText('40')).toBeInTheDocument();
    expect(within(panel).getByText('71.4')).toBeInTheDocument(); // the algorithmic score is displayed untouched
    expect(within(panel).getByText('Material disagreement')).toBeInTheDocument();
    expect(within(panel).getByText(/31.4 points below/)).toBeInTheDocument();
    expect(within(panel).getByText('The AI weights cash flow more heavily.')).toBeInTheDocument();
    expect(within(panel).getByText('Technical data is stale.')).toBeInTheDocument();
    expect(within(panel).getByRole('button', { name: 'Analysis stored' })).toBeDisabled(); // no repeat call
  });

  it('shows a stored result without calling the API again', async () => {
    mocked.mockResolvedValueOnce(status({ analyses: { positional: aiResult({ cached: true }) } }));
    render(<AiAnalysisPanel brief={assessed()} horizon="positional" />);
    expect(await screen.findByText(/stored result \(no new API call\)/)).toBeInTheDocument();
    expect(mocked).toHaveBeenCalledTimes(1);
  });

  it('reports an API failure and allows a retry', async () => {
    mocked.mockResolvedValueOnce(status()).mockRejectedValueOnce(new Error('The AI provider rate limit or quota was reached.'));
    render(<AiAnalysisPanel brief={assessed()} horizon="positional" />);
    const button = await screen.findByRole('button', { name: 'Run AI Analysis' });
    await waitFor(() => expect(button).toBeEnabled());
    fireEvent.click(button);
    expect(await screen.findByRole('alert')).toHaveTextContent(/rate limit/);
    expect(screen.getByRole('button', { name: 'Run AI Analysis' })).toBeEnabled();
  });

  it('labels a no-evidence result as not sent to the provider and does not invent a score', async () => {
    mocked.mockResolvedValueOnce(status({ analyses: { positional: aiResult({ source: 'local_gate', comparison: { algorithmic_score: null, algorithmic_recommendation: null, ai_score: null, ai_recommendation: 'INSUFFICIENT_EVIDENCE', score_gap: null, level: 'not_comparable', recommendation_differs: null, note: 'No algorithmic score exists for this horizon; the two assessments cannot be compared numerically.' },
      result: { ...aiResult().result, score: null, recommendation: 'INSUFFICIENT_EVIDENCE', confidence: 0 } }) } }));
    render(<AiAnalysisPanel brief={assessed()} horizon="positional" />);
    expect(await screen.findByText(/No AI call was made/)).toBeInTheDocument();
    expect(screen.getByText('Not comparable')).toBeInTheDocument();
  });
});


describe('Compact layout controls', () => {
  const mocked = vi.mocked(apiRequest);
  afterEach(() => mocked.mockReset());

  it('collapses long AI lists and reasoning behind Show more / Show less without dropping any item', async () => {
    const items = ['One', 'Two', 'Three', 'Four', 'Five'];
    mocked.mockResolvedValueOnce({ enabled: true, configured: true, provider: 'gemini', model: 'm', brief_id: 'b', analyses: { positional: {
      id: 'a', brief_id: 'b', horizon: 'positional', model: 'm', prompt_version: 'v3', snapshot_hash: 'abcdef1234567890', provider: 'gemini', source: 'gemini', created_at: '2026-11-05T05:00:00Z', cached: true,
      disclaimer: 'd', usage: null, comparison: null,
      result: { score: 50, recommendation: 'HOLD', confidence: 50, summary: 'S', reasoning: 'R'.repeat(400), strengths: items, weaknesses: [], risks: [],
        bull_case: { thesis: 'b', drivers: [], invalidated_by: [] }, bear_case: { thesis: 'b', drivers: [], invalidated_by: [] }, data_gaps: [], disagreement_explanation: '', guardrails_applied: [] } } } });
    render(<AiAnalysisPanel brief={brief(assessment())} horizon="positional" />);
    expect(await screen.findByText('Three')).toBeInTheDocument();
    expect(screen.queryByText('Four')).toBeNull();
    const buttons = screen.getAllByRole('button', { name: /Show/ });
    fireEvent.click(screen.getByRole('button', { name: 'Show 2 more' }));
    expect(screen.getByText('Five')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Show less' })).toBeInTheDocument();
    expect(buttons.some((b) => b.textContent === 'Show more')).toBe(true); // the long reasoning is clamped with its own toggle
  });

  it('shows valuation blockers concisely in the overview with the detailed reasons expandable', () => {
    const base = assessment();
    const withValuation = { ...base, components: { ...base.components, valuation: component('valuation', { status: 'blocked', score: null, interpretation: null, confidence: null, blockers: ['Only 0 groups scored.', 'Why: no comparable peers.'] }) } } as Assessment;
    render(<ResearchBriefView brief={brief(withValuation)} instrument={null} />);
    const summaries = screen.getAllByText(/Insufficient evidence — details/); // technical, news and valuation are all blocked in this fixture
    expect(summaries.length).toBeGreaterThanOrEqual(3);
    summaries.forEach((summary) => expect(summary.closest('details')).not.toHaveAttribute('open'));
    expect(screen.getAllByText('Why: no comparable peers.').length).toBeGreaterThan(0); // present in the DOM, collapsed
  });

  it('keeps every Research tab reachable and switchable', () => {
    render(<ResearchBriefView brief={brief(assessment())} instrument={null} />);
    const names = screen.getAllByRole('tab').filter((t) => t.id.startsWith('rt-tab-')).map((t) => t.textContent);
    expect(names).toEqual(['Overview', 'Fundamentals', 'Valuation', 'Shareholding', 'Financial Statements', 'Technical Analysis', 'News', 'Sentiment', 'Risks & Scenarios', 'Data Quality / Sources']);
    for (const name of names) {
      fireEvent.click(screen.getByRole('tab', { name: name as string }));
      expect(screen.getByRole('tab', { name: name as string })).toHaveAttribute('aria-selected', 'true');
    }
  });
});

describe('Research horizons', () => {
  it('offers only Swing, Positional and Long-term, even for a stored report that still contains an intraday assessment', () => {
    const legacy = assessment();
    (legacy.horizons as Record<string, unknown>).intraday = horizon('swing', { score: 12, recommendation: 'AVOID' });
    (legacy.weights as Record<string, unknown>).intraday = { fundamental: 0.05, technical: 0.55, financial_trends: 0.1, news_sentiment: 0.3 };
    render(<ResearchBriefView brief={brief(legacy)} instrument={null} />);
    const tabs = screen.getAllByRole('tab').filter((tab) => tab.id.startsWith('rt-horizon-'));
    expect(tabs.map((tab) => tab.id)).toEqual(['rt-horizon-swing', 'rt-horizon-positional', 'rt-horizon-long_term']);
    expect(screen.queryByText(/Intraday/i)).toBeNull();
  });
});

describe('Valuation tab', () => {
  const metric = (key: string, label: string, over: Record<string, unknown> = {}) => ({
    key, label, status: 'calculated', value: '10.00', unit: 'x', origin: 'calculated', provider: null, reporting_period: '2026-09-30', retrieved_at: '2026-11-05T05:00:00Z',
    reason: null, basis: 'market cap / net income (TTM)', inputs: [], warnings: [], freshness: 'current', statement_basis: 'consolidated', cross_check: null, ...over,
  });
  const valuation = () => ({
    model_version: 'valuation-evidence-0.2.0', currency: 'INR', as_of: '2026-11-05T05:00:00Z',
    scoring: { version: 'valuation-score-0.2.0-provisional', status: 'blocked', score: null, label: null, confidence: null, coverage_pct: 0, sector_family: 'default', blockers: ['No peers.'] },
    price: { value: '100', as_of: '2026-11-04', current: true, problem: null }, data_gaps: ['cannot be calculated without: cash and equivalents (not stored by any current provider)'],
    metrics: {
      market_cap: metric('market_cap', 'Market capitalisation', { value: '2400000000', unit: 'INR', origin: 'provider_reported', provider: 'dhan' }),
      pe_ttm: metric('pe_ttm', 'P/E', { value: '9.50', origin: 'provider_reported', provider: 'yfinance' }),
      pb: metric('pb', 'P/B', { status: 'not_meaningful', value: null, origin: null, reason: "shareholders' equity is not positive (-5); P/B is not meaningful" }),
      enterprise_value: metric('enterprise_value', 'Enterprise value', { status: 'unavailable', value: null, origin: null, reason: 'cannot be calculated without: cash and equivalents (not stored by any current provider)' }),
      roe: metric('roe', 'ROE', { value: '20.00', unit: '%' }),
    },
    peer_comparison: { status: 'insufficient_peers', peer_group: 'industry: Banks', note: 'n', metrics: { pe_ttm: { peer_count: 2, status: 'insufficient_peers', reason: 'only 2 comparable peer(s) (minimum 5)' } } },
  });

  it('shows a clean dashboard: available metrics only, subtle source labels, one note, provenance collapsed', () => {
    const b = brief(assessment());
    (b.sections as unknown as Record<string, unknown>).valuation = valuation();
    render(<ResearchBriefView brief={b} instrument={null} />);
    fireEvent.click(screen.getByRole('tab', { name: 'Valuation' }));
    expect(screen.getByText('Financial quality')).toBeInTheDocument();
    expect(screen.getByText('₹240 Cr')).toBeInTheDocument();
    expect(screen.getByText('9.50x')).toBeInTheDocument();
    expect(screen.getByText('20.00%')).toBeInTheDocument();
    expect(screen.getByText('Yahoo')).toBeInTheDocument();
    expect(screen.getByText('Dhan')).toBeInTheDocument();
    expect(screen.getByText(/contributes to Research scoring only where sufficient reliable evidence/)).toBeInTheDocument();
    expect(screen.queryByText('Unavailable')).toBeNull(); // no placeholder cards
    expect(document.querySelectorAll('.rt-vcard').length).toBe(3); // market cap, P/E, ROE; the unavailable and not-meaningful metrics get no card
    expect(screen.getByText('Sources, freshness and methodology').closest('details')).not.toHaveAttribute('open');
    expect(screen.getByText(/cannot be calculated without: cash and equivalents/)).toBeInTheDocument(); // reasons stay in the details list
  });

  it('renders a missing peer percentile and a missing score as unknown, never as 0 / cheapest', () => {
    const b = brief(assessment());
    const v = valuation();
    (v as unknown as { peer_comparison: unknown }).peer_comparison = { status: 'compared', peer_group: 'industry: Banks', note: 'n',
      metrics: { pe_ttm: { peer_count: 8, status: 'compared', subject: '9.5', peer_median: '12' } } };
    (v as unknown as { scoring: unknown }).scoring = { version: 'v', status: 'scored', score: null, label: 'x', confidence: 50, coverage_pct: 100, sector_family: 'default', blockers: [] };
    (b.sections as unknown as Record<string, unknown>).valuation = v;
    render(<ResearchBriefView brief={b} instrument={null} />);
    fireEvent.click(screen.getByRole('tab', { name: 'Valuation' }));
    expect(screen.getByText(/percentile —/)).toBeInTheDocument();
    expect(screen.queryByText(/cheaper than/)).toBeNull();
    expect(document.querySelector('.rt-vpeer-dot')).toBeNull();
    expect(screen.queryByText(/Score 0/)).toBeNull();
    expect(screen.getByText(/Score — · x/)).toBeInTheDocument();
  });

  it('shows the valuation score when the production component is scored', () => {
    const b = brief(assessment());
    const v = valuation();
    (v as unknown as { scoring: unknown }).scoring = { version: 'valuation-score-0.2.0-provisional', status: 'scored', score: 72.4, label: 'attractive', confidence: 60, coverage_pct: 100, sector_family: 'default', blockers: [] };
    (b.sections as unknown as Record<string, unknown>).valuation = v;
    render(<ResearchBriefView brief={b} instrument={null} />);
    fireEvent.click(screen.getByRole('tab', { name: 'Valuation' }));
    expect(screen.getByText(/Score 72 · attractive/)).toBeInTheDocument();
  });

  it('explains reports generated before valuation existed and still renders a model-0.1.0 assessment without a valuation component', () => {
    render(<ResearchBriefView brief={brief(assessment())} instrument={null} />);
    expect(screen.queryByText('Valuation Intelligence')).toBeNull();
    fireEvent.click(screen.getByRole('tab', { name: 'Valuation' }));
    expect(screen.getByText(/before Valuation Intelligence existed/)).toBeInTheDocument();
  });
});

describe('Shareholding section', () => {
  const shareholding = (over: Record<string, unknown> = {}) => ({
    status: 'available', source: 'dhan', retrieved_at: '2026-11-05T05:00:00+00:00', reporting_period: null, reporting_period_note: 'Not provided by source',
    holdings: [
      { key: 'promoter', label: 'Promoter', percent: '50.44', unit: 'percent' }, { key: 'fii', label: 'Foreign institutional investors (FII)', percent: '16.67', unit: 'percent' },
      { key: 'dii', label: 'Domestic institutional investors (DII)', percent: '21.72', unit: 'percent' }, { key: 'public', label: 'Public', percent: null, unit: 'percent' },
    ],
    sum_percent: '88.83', not_itemised_percent: '11.17',
    warnings: ['Dhan returns only these four categories and no reporting period. The categories are incomplete and do not necessarily sum to 100%, and the figures must not be read as the latest quarter\'s pattern.'],
    ...over,
  });

  it('shows each category, source Dhan, the actual retrieval time, and that no reporting period was provided', () => {
    const b = brief(assessment());
    (b.sections as unknown as Record<string, unknown>).shareholding = shareholding();
    render(<ResearchBriefView brief={b} instrument={null} />);
    fireEvent.click(screen.getByRole('tab', { name: 'Shareholding' }));
    expect(screen.getByText('50.44%')).toBeInTheDocument();
    expect(screen.getByText('16.67%')).toBeInTheDocument();
    expect(screen.getByText('Not provided')).toBeInTheDocument();  // a null category is not shown as 0%
    expect(screen.getAllByText('Dhan').length).toBe(4);
    expect(screen.getAllByText(/IST/).length).toBeGreaterThan(0);
    expect(screen.getAllByText('Not provided by source').length).toBeGreaterThan(0);
    expect(screen.getByText(/must not be read as the latest quarter's shareholding pattern/)).toBeInTheDocument();
    expect(screen.getByText(/categories are incomplete/)).toBeInTheDocument();
    expect(screen.getByText(/Not itemised by the source: 11\.17%/)).toBeInTheDocument();
  });

  it('explains when shareholding data was not retrieved, and for reports from before it existed', () => {
    const b = brief(assessment());
    (b.sections as unknown as Record<string, unknown>).shareholding = shareholding({ status: 'unavailable', holdings: [], warnings: ['No shareholding data was retrieved (Dhan Company Info unavailable or not mapped).'] });
    render(<ResearchBriefView brief={b} instrument={null} />);
    fireEvent.click(screen.getByRole('tab', { name: 'Shareholding' }));
    expect(screen.getByText(/No shareholding data was retrieved/)).toBeInTheDocument();
    cleanup();
    render(<ResearchBriefView brief={brief(assessment())} instrument={null} />);
    fireEvent.click(screen.getByRole('tab', { name: 'Shareholding' }));
    expect(screen.getByText(/before shareholding data was collected/)).toBeInTheDocument();
  });
});
