import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import type { Assessment, Brief, ComponentKey, HorizonAssessment, HorizonKey, ScoreComponent } from './researchTypes';

vi.mock('../components/CandleChart', () => ({ CandleChart: () => null, sessionCloseForVenue: () => null }));
vi.mock('../api', () => ({ apiRequest: vi.fn(() => new Promise(() => undefined)), ApiRequestError: class extends Error {} }));

import { ResearchBriefView, plainText } from './ResearchReport';

const component = (key: ComponentKey, over: Partial<ScoreComponent> = {}): ScoreComponent => ({
  key, label: `${key} Intelligence`, status: 'scored', score: 72, interpretation: 'strong', confidence: 66, coverage_pct: 80,
  factors: [{ key: 'f', label: 'Return on equity', value: 18.2, unit: '%', sub_score: 78, basis: 'b' }],
  missing: [], conflicts: [], blockers: [], risk_flags: [], notes: [], ...over,
});
const horizon = (key: HorizonKey, over: Partial<HorizonAssessment> = {}): HorizonAssessment => ({
  horizon: key, label: key, weights: { fundamental: 0.25, technical: 0.25, financial_trends: 0.3, news_sentiment: 0.2 }, scored_weight: 1,
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
  },
  horizons: {
    intraday: horizon('intraday', { score: null, recommendation: 'INSUFFICIENT_EVIDENCE', raw_recommendation: null, confidence: null, blockers: ['Intraday scoring needs intraday-bar indicators.'] }),
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
    fireEvent.click(screen.getByRole('tab', { name: /Intraday/ }));
    expect(screen.getByText('Intraday scoring needs intraday-bar indicators.')).toBeInTheDocument();
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
