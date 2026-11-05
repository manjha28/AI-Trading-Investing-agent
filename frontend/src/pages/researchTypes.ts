export type Instrument = {
  id: string; symbol: string; display_name: string; venue: string; is_synthetic: boolean;
  tick_size?: string | null; currency?: string; timezone?: string; asset_class?: string;
};
export type WatchlistEntry = { instrument_id: string; symbol: string };
export type SourceLink = { id: string; instrument_id: string; url: string; label: string; enabled: boolean; created_at: string };
export type Source = {
  id: string;
  instrument_id: string | null;
  url: string;
  publisher: string | null;
  title: string | null;
  fetched_at: string;
  deduplicated?: boolean;
};
export type Fact = { statement: string; as_of: string; source_ids: string[]; stale: boolean };
export type Note = {
  id: string;
  instrument_id: string;
  as_of: string;
  summary: string;
  key_facts: Fact[];
  source_ids: string[];
  confidence: 'low' | 'medium' | 'high';
  stale: boolean;
};
export type FieldValue = { value: string | null; field_path: string };
export type FundamentalPeriod = {
  period_type: 'annual' | 'quarterly';
  period_end: string;
  currency: string;
  values: Record<string, FieldValue>;
  source: string;
  fetched_at: string;
  stale: boolean;
};
export type IndicatorValue = {
  value: string | null;
  is_synthetic: boolean;
  bars_required: number;
  bars_used: number;
  field_path: string;
};
export type NewsItem = {
  headline: string;
  publisher: string;
  published_at: string | null;
  url: string;
  source_id: string;
};
export type StoredNewsItem = {
  external_id: string; source: string; source_url: string | null; instrument_id: string | null;
  symbol_raw: string | null; published_at: string; retrieved_at: string; publisher: string | null;
  category: string | null; headline: string; body: string | null; full_text_status: string;
  published_time_kind: string;
};
export type NewsSourceHealth = {
  source: string; status: string; last_attempted_at: string; last_successful_poll_at: string | null;
  last_successful_article_at: string | null; articles_discovered: number; articles_inserted: number;
  duplicate_count: number; failure_count: number; last_error_category: string | null; rate_limited: boolean;
};
export type SentimentArticle = {
  news_item_id: string; source: string; publisher: string | null; url: string | null;
  headline: string; summary: string | null; published_at: string; instrument_id: string | null;
  symbol_raw: string | null; category: string | null; full_text_status: string;
  sentiment: 'positive' | 'negative' | 'neutral' | 'mixed'; score: number; strength: number;
  impact: 'low' | 'medium' | 'high'; confidence_score: number; event_category: string;
  relevance_scope: string; interpretation: string; explanation: string;
  evidence: { field: string; text: string }[]; event_key: string; classifier_version: string; analyzed_at: string;
};
export type SentimentEvent = {
  event_key: string; sentiment: string; score: number; confidence_score: number; impact: string;
  event_category: string; relevance_scope: string; contradictory_sources: boolean;
  news_item_ids: string[]; sources: string[]; items: SentimentArticle[];
};
export type SentimentAggregate = {
  status: 'available' | 'stale' | 'not_analyzed' | 'analysis_failed' | 'source_unavailable' | 'no_news_found' | 'successful_empty' | string;
  scope: string; scope_id: string | null; sentiment: 'positive' | 'negative' | 'neutral' | 'mixed' | null; interpretation: string | null;
  score: number | null; strength: number | null; confidence_score: number | null;
  event_count: number; article_count: number; counts: Record<string, number>;
  contradictory_evidence: boolean; freshness: { latest_published_at: string | null; age_hours: number | null; is_stale: boolean };
  coverage: { period_days: number; analyzed_events: number; unanalysed_news_items: number; available_source_fraction: number; analysis: { available_items: number; analyzed_items: number; pending_items: number; failed_items: number }; sources: { source: string; status: string; rate_limited: boolean; last_error_category: string | null }[] };
  events: SentimentEvent[]; instrument?: { id: string; symbol: string; name: string; sector: string | null; industry: string | null };
};
export type SentimentHistoryPoint = { date: string; score: number; event_count: number; article_count: number };
export type NewsSource = {
  source_id: string;
  title: string;
  publisher: string;
  url: string;
  fetched_at: string;
};
export type QualityItem = {
  kind: string;
  field_path: string;
  reason: string;
  stage?: string;
  url?: string;
};
export type Brief = {
  run_id: string;
  instrument_id: string;
  status: 'running' | 'complete' | 'partial' | 'failed';
  started_at: string;
  finished_at: string | null;
  sources_attempted: number;
  sources_succeeded: number;
  failures: { source: string; stage: string; url: string; reason: string }[];
  renderer: 'template' | 'ai';
  created_at: string;
  data_quality: QualityItem[];
  sections: {
    fundamentals: {
      status: string;
      snapshot: {
        status: string;
        values: Record<string, FieldValue> | null;
        source?: string;
        fetched_at?: string;
        stale?: boolean;
        comparison_status?: string;
        conflicts?: { field: string; source_a: string; value_a: string; source_b: string; value_b: string }[];
      };
      periods: FundamentalPeriod[];
    };
    technicals: {
      status: string;
      message: string | null;
      interval: string;
      source: string | null;
      latest_bar: string | null;
      latest_close?: string | null;
      staleness?: { stale: boolean; basis: string; latest_session: string | null; expected_session: string | null; reason: string | null } | null;
      bars_available: number;
      indicators: Record<string, IndicatorValue>;
    };
    financials_trend: {
      status: string;
      periods: {
        period_type: string;
        period_end: string;
        currency: string;
        values: Record<string, { value: string | null; year_on_year_growth_pct: string | null; field_paths: string[] }>;
      }[];
    };
    news: { status: string; message: string | null; items: NewsItem[]; sources: NewsSource[] };
    news_sentiment?: SentimentAggregate & { validation_status?: string; classifier_version?: string };
    collector_status?: Record<string, { state: string; detail?: string; source?: string | null; records?: number; retrieved_at?: string | null; source_count?: number; sources?: string[] }>;
    data_quality: { status: string; items: QualityItem[] };
    assessment?: Assessment;
    disclaimer: string;
  };
};
export type FactDraft = { statement: string; as_of: string; source_ids: string[] };
export type ResearchProgressEvent = { component: string; state: string; detail: string; updated_at: string; source?: string | null; retrieved_at?: string | null; [key: string]: unknown };
export type ResearchRunState = {
  run_id: string; instrument_id: string; status: Brief['status'] | 'cancelled';
  started_at: string; finished_at: string | null; sources_attempted: number; sources_succeeded: number;
  failures: Brief['failures']; progress: ResearchProgressEvent[]; brief: Brief | null; reused: boolean;
};
export type BatchRunResult = {
  instrument: Instrument;
  brief: Brief | null;
  error: string | null;
  status?: ResearchRunState['status'] | 'preparing';
  run_id?: string;
  progress?: ResearchProgressEvent[];
};

export type ComponentKey = 'fundamental' | 'technical' | 'financial_trends' | 'news_sentiment';
export type HorizonKey = 'intraday' | 'swing' | 'positional' | 'long_term';
export type ScoreFactor = { key: string; label: string; value: number; unit: string; sub_score: number; basis: string };
export type ScoreComponent = {
  key: ComponentKey; label: string; status: 'scored' | 'blocked'; score: number | null; interpretation: string | null;
  confidence: number | null; coverage_pct: number | null; factors: ScoreFactor[]; missing: string[]; conflicts: string[];
  blockers: string[]; risk_flags: string[]; notes: string[];
};
export type HorizonAssessment = {
  horizon: HorizonKey; label: string; weights: Record<ComponentKey, number>; scored_weight: number; blockers: string[];
  score: number | null; recommendation: 'BUY' | 'HOLD' | 'AVOID' | 'WAIT' | 'INSUFFICIENT_EVIDENCE'; raw_recommendation: string | null;
  confidence: number | null; components_used: ComponentKey[]; components_missing: ComponentKey[];
  gates: { code: string; detail: string }[]; contributions: { component: ComponentKey; weight: number; effective_weight: number; score: number }[];
};
export type Assessment = {
  error?: string; model_version: string; generated_at: string; default_horizon: HorizonKey;
  validation: { status: string; backtested: boolean; statement: string };
  weights: Record<HorizonKey, Record<ComponentKey, number>>; thresholds: Record<string, number>;
  sector: string | null; financial_sector: boolean; not_implemented_gates: string[];
  components: Record<ComponentKey, ScoreComponent>; horizons: Record<HorizonKey, HorizonAssessment>;
};
