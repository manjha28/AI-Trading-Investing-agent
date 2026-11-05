import { useCallback, useEffect, useState, type FormEvent } from 'react';
import { ApiRequestError, apiRequest } from '../api';

type Instrument = { id: string; symbol: string; display_name: string; venue: string; is_synthetic: boolean };
type WatchlistEntry = { instrument_id: string; symbol: string };
type SourceLink = { id: string; instrument_id: string; url: string; label: string; enabled: boolean; created_at: string };
type Source = {
  id: string;
  instrument_id: string | null;
  url: string;
  publisher: string | null;
  title: string | null;
  fetched_at: string;
  deduplicated?: boolean;
};
type Fact = { statement: string; as_of: string; source_ids: string[]; stale: boolean };
type Note = {
  id: string;
  instrument_id: string;
  as_of: string;
  summary: string;
  key_facts: Fact[];
  source_ids: string[];
  confidence: 'low' | 'medium' | 'high';
  stale: boolean;
};
type FieldValue = { value: string | null; field_path: string };
type FundamentalPeriod = {
  period_type: 'annual' | 'quarterly';
  period_end: string;
  currency: string;
  values: Record<string, FieldValue>;
  source: string;
  fetched_at: string;
  stale: boolean;
};
type IndicatorValue = {
  value: string | null;
  is_synthetic: boolean;
  bars_required: number;
  bars_used: number;
  field_path: string;
};
type NewsItem = {
  headline: string;
  publisher: string;
  published_at: string | null;
  url: string;
  source_id: string;
};
type StoredNewsItem = {
  external_id: string; source: string; source_url: string | null; instrument_id: string | null;
  symbol_raw: string | null; published_at: string; retrieved_at: string; publisher: string | null;
  category: string | null; headline: string; body: string | null; full_text_status: string;
  published_time_kind: string;
};
type NewsSourceHealth = {
  source: string; status: string; last_attempted_at: string; last_successful_poll_at: string | null;
  last_successful_article_at: string | null; articles_discovered: number; articles_inserted: number;
  duplicate_count: number; failure_count: number; last_error_category: string | null; rate_limited: boolean;
};
type SentimentArticle = {
  news_item_id: string; source: string; publisher: string | null; url: string | null;
  headline: string; summary: string | null; published_at: string; instrument_id: string | null;
  symbol_raw: string | null; category: string | null; full_text_status: string;
  sentiment: 'positive' | 'negative' | 'neutral' | 'mixed'; score: number; strength: number;
  impact: 'low' | 'medium' | 'high'; confidence_score: number; event_category: string;
  relevance_scope: string; interpretation: string; explanation: string;
  evidence: { field: string; text: string }[]; event_key: string; classifier_version: string; analyzed_at: string;
};
type SentimentEvent = {
  event_key: string; sentiment: string; score: number; confidence_score: number; impact: string;
  event_category: string; relevance_scope: string; contradictory_sources: boolean;
  news_item_ids: string[]; sources: string[]; items: SentimentArticle[];
};
type SentimentAggregate = {
  status: 'available' | 'stale' | 'not_analyzed' | 'analysis_failed' | 'source_unavailable' | 'no_news_found' | 'successful_empty' | string;
  scope: string; scope_id: string | null; sentiment: 'positive' | 'negative' | 'neutral' | 'mixed' | null; interpretation: string | null;
  score: number | null; strength: number | null; confidence_score: number | null;
  event_count: number; article_count: number; counts: Record<string, number>;
  contradictory_evidence: boolean; freshness: { latest_published_at: string | null; age_hours: number | null; is_stale: boolean };
  coverage: { period_days: number; analyzed_events: number; unanalysed_news_items: number; available_source_fraction: number; analysis: { available_items: number; analyzed_items: number; pending_items: number; failed_items: number }; sources: { source: string; status: string; rate_limited: boolean; last_error_category: string | null }[] };
  events: SentimentEvent[]; instrument?: { id: string; symbol: string; name: string; sector: string | null; industry: string | null };
};
type SentimentHistoryPoint = { date: string; score: number; event_count: number; article_count: number };
type NewsSource = {
  source_id: string;
  title: string;
  publisher: string;
  url: string;
  fetched_at: string;
};
type QualityItem = {
  kind: string;
  field_path: string;
  reason: string;
  stage?: string;
  url?: string;
};
type Brief = {
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
    disclaimer: string;
  };
};
type FactDraft = { statement: string; as_of: string; source_ids: string[] };
type ResearchProgressEvent = { component: string; state: string; detail: string; updated_at: string; source?: string | null; retrieved_at?: string | null; [key: string]: unknown };
type ResearchRunState = {
  run_id: string; instrument_id: string; status: Brief['status'] | 'cancelled';
  started_at: string; finished_at: string | null; sources_attempted: number; sources_succeeded: number;
  failures: Brief['failures']; progress: ResearchProgressEvent[]; brief: Brief | null; reused: boolean;
};
type BatchRunResult = {
  instrument: Instrument;
  brief: Brief | null;
  error: string | null;
  status?: ResearchRunState['status'] | 'preparing';
  run_id?: string;
  progress?: ResearchProgressEvent[];
};

const today = () => new Date().toISOString().slice(0, 10);
const MAX_BATCH_INSTRUMENTS = 5;

function sourceFailureSummary(failure: Brief['failures'][number]): string {
  const reason = failure.reason.toLowerCase();
  if (reason.includes('robots.txt')) {
    return 'The site does not permit this URL to be fetched. Choose an authorized RSS or investor-relations feed.';
  }
  if (reason.includes('429') || reason.includes('rate limit')) {
    return 'The source is rate-limiting requests. Retry later or use a permitted feed endpoint.';
  }
  if (reason.includes('timed out') || reason.includes('timeout')) {
    return 'The source did not respond in time. Verify the feed URL or try again later.';
  }
  if (reason.includes('connection')) {
    return 'The source could not be reached. Verify the URL and provider availability.';
  }
  return 'This source could not be fetched. Verify that the URL is a public, permitted feed.';
}

function sentimentStatusText(aggregate: SentimentAggregate | null): string {
  if (!aggregate) return 'Sentiment data is loading.';
  if (aggregate.status === 'no_news_found') return 'No news found in the selected period.';
  if (aggregate.status === 'not_analyzed') return 'News is available but has not yet been analyzed.';
  if (aggregate.status === 'analysis_failed') return 'Analysis failed for available news; retry will occur automatically.';
  if (aggregate.status === 'source_unavailable') return 'News sources are unavailable; no sentiment score is reported.';
  if (aggregate.status === 'stale') return 'Analyzed news is stale; treat this context as uncertain.';
  if (aggregate.confidence_score !== null && aggregate.confidence_score < 45) return 'Analysis is uncertain because evidence or source coverage is limited.';
  return 'Sentiment is derived from persisted headlines and permitted summaries.';
}

function scoreLabel(score: number | null): string {
  return score === null ? 'Unavailable' : `${score > 0 ? '+' : ''}${score.toFixed(3)} / 1.000`;
}

export function Research({ watchlist, addInstrumentToWatchlist, removeInstrumentFromWatchlist }: {
  watchlist: WatchlistEntry[];
  addInstrumentToWatchlist: (instrumentId: string, symbol: string) => Promise<void>;
  removeInstrumentFromWatchlist: (instrumentId: string) => Promise<void>;
}) {
  const [instrumentQuery, setInstrumentQuery] = useState('');
  const [instruments, setInstruments] = useState<Instrument[]>([]);
  const [batchSelection, setBatchSelection] = useState<Instrument[]>([]);
  const [batchResults, setBatchResults] = useState<BatchRunResult[]>([]);
  const [selected, setSelected] = useState<Instrument | null>(null);
  const [brief, setBrief] = useState<Brief | null>(null);
  const [sourceLinks, setSourceLinks] = useState<SourceLink[]>([]);
  const [legacySources, setLegacySources] = useState<Source[]>([]);
  const [notes, setNotes] = useState<Note[]>([]);
  const [sourceUrl, setSourceUrl] = useState('');
  const [sourceLabel, setSourceLabel] = useState('');
  const [legacySourceUrl, setLegacySourceUrl] = useState('');
  const [noteAsOf, setNoteAsOf] = useState(today());
  const [noteSummary, setNoteSummary] = useState('');
  const [noteConfidence, setNoteConfidence] = useState<Note['confidence']>('medium');
  const [noteFacts, setNoteFacts] = useState<FactDraft[]>([{ statement: '', as_of: today(), source_ids: [] }]);
  const [editingNote, setEditingNote] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [searching, setSearching] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [storedNews, setStoredNews] = useState<StoredNewsItem[]>([]);
  const [storedNewsError, setStoredNewsError] = useState('');
  const [newsHealth, setNewsHealth] = useState<NewsSourceHealth[]>([]);
  const [marketSentiment, setMarketSentiment] = useState<SentimentAggregate | null>(null);
  const [instrumentSentiments, setInstrumentSentiments] = useState<SentimentAggregate[]>([]);
  const [sectorSentiments, setSectorSentiments] = useState<SentimentAggregate[]>([]);
  const [recentSentiment, setRecentSentiment] = useState<SentimentArticle[]>([]);
  const [selectedSentiment, setSelectedSentiment] = useState<SentimentAggregate | null>(null);
  const [selectedSentimentHistory, setSelectedSentimentHistory] = useState<SentimentHistoryPoint[]>([]);
  const [sentimentBusy, setSentimentBusy] = useState(false);
  const [sentimentError, setSentimentError] = useState('');

  const refreshSentiment = useCallback(async () => {
    const requests = await Promise.allSettled([
      apiRequest<SentimentAggregate>('/news/sentiment/market?period_days=7'),
      apiRequest<SentimentAggregate[]>('/news/sentiment/instruments?period_days=7&limit=100'),
      apiRequest<SentimentAggregate[]>('/news/sentiment/sectors?period_days=7'),
      apiRequest<SentimentArticle[]>('/news/sentiment/recent?limit=30&period_days=30'),
    ]);
    if (requests[0].status === 'fulfilled') setMarketSentiment(requests[0].value);
    if (requests[1].status === 'fulfilled') setInstrumentSentiments(requests[1].value);
    if (requests[2].status === 'fulfilled') setSectorSentiments(requests[2].value);
    if (requests[3].status === 'fulfilled') setRecentSentiment(requests[3].value);
    const failed = requests.find((request) => request.status === 'rejected');
    setSentimentError(failed?.status === 'rejected' && failed.reason instanceof Error ? failed.reason.message : '');
  }, []);

  useEffect(() => {
    let active = true;
    void apiRequest<StoredNewsItem[]>('/news?limit=50').then((items) => {
      if (active) setStoredNews(items);
    }).catch((requestError: Error) => {
      if (active) setStoredNewsError(requestError.message);
    });
    void apiRequest<NewsSourceHealth[]>('/news/sources/health').then((rows) => {
      if (active) setNewsHealth(rows);
    }).catch(() => undefined);
    void refreshSentiment();
    return () => { active = false; };
  }, [refreshSentiment]);

  useEffect(() => {
    if (!selected) {
      setSelectedSentiment(null);
      setSelectedSentimentHistory([]);
      return;
    }
    let active = true;
    void Promise.allSettled([
      apiRequest<SentimentAggregate>(`/news/sentiment/instruments/${selected.id}?period_days=7`),
      apiRequest<SentimentHistoryPoint[]>(`/news/sentiment/history?period_days=90&instrument_id=${selected.id}`),
    ]).then(([summary, history]) => {
      if (active && summary.status === 'fulfilled') setSelectedSentiment(summary.value);
      if (active && history.status === 'fulfilled') setSelectedSentimentHistory(history.value);
    });
    return () => { active = false; };
  }, [selected?.id]);

  async function runSentimentAnalysis() {
    setSentimentBusy(true);
    setSentimentError('');
    try {
      const result = await apiRequest<{ examined: number; analyzed: number; already_analyzed: number }>(
        '/news/sentiment/analyze?limit=500&period_days=30', { method: 'POST' },
      );
      setNotice(`News analysis processed ${result.analyzed} items; ${result.already_analyzed} already had this classifier version.`);
      await refreshSentiment();
    } catch (requestError) {
      setSentimentError(requestError instanceof Error ? requestError.message : 'Sentiment analysis failed.');
    } finally {
      setSentimentBusy(false);
    }
  }

  const refreshNotesAndSources = useCallback(async (instrumentId: string) => {
    const [detail, links] = await Promise.all([
      apiRequest<{ notes: Note[]; sources: Source[] }>(`/research/instruments/${instrumentId}`),
      apiRequest<SourceLink[]>(`/research/instruments/${instrumentId}/source-links`),
    ]);
    setNotes(detail.notes);
    setLegacySources(detail.sources);
    setSourceLinks(links);
  }, []);

  useEffect(() => {
    if (!selected) {
      setBrief(null);
      setNotes([]);
      setLegacySources([]);
      setSourceLinks([]);
      return;
    }
    let active = true;
    setError('');
    void Promise.all([
      apiRequest<Brief>(`/research/instruments/${selected.id}/brief`).catch((requestError: unknown) => {
        if (requestError instanceof ApiRequestError && requestError.status === 404) return null;
        throw requestError;
      }),
      refreshNotesAndSources(selected.id),
    ]).then(([latest]) => {
      if (active) setBrief(latest);
    }).catch((requestError: Error) => {
      if (active) setError(requestError.message);
    });
    return () => {
      active = false;
    };
  }, [selected?.id, refreshNotesAndSources]);

  async function searchInstruments(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!instrumentQuery.trim()) return;
    setSearching(true);
    setError('');
    try {
      const query = new URLSearchParams({ query: instrumentQuery.trim(), limit: '30' });
      setInstruments(await apiRequest<Instrument[]>(`/market-data/instruments?${query}`));
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : 'Instrument search failed.');
    } finally {
      setSearching(false);
    }
  }

  async function runResearch() {
    if (!selected) return;
    await runResearchBatch([selected]);
  }

  async function refreshSelectedData() {
    if (!selected || selected.is_synthetic) return;
    setBusy(true);
    setError('');
    setNotice('');
    try {
      const now = new Date();
      const start = new Date(now);
      start.setUTCFullYear(start.getUTCFullYear() - 2);
      const query = new URLSearchParams({
        start: start.toISOString(), end: now.toISOString(), interval: '1d', limit: '500',
      });
      const result = await apiRequest<{ bars: unknown[] }>(
        `/market-data/instruments/${selected.id}/history/sync?${query}`, { method: 'POST' },
      );
      setNotice(result.bars.length
        ? `History sync returned ${result.bars.length} stored or refreshed daily bars for ${selected.symbol}.`
        : `History sync returned no daily bars for ${selected.symbol}; existing report data is unchanged.`);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : 'Data refresh failed.');
    } finally {
      setBusy(false);
    }
  }

  function viewPreviousReport() {
    document.getElementById('research-report')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  function toggleBatchInstrument(instrument: Instrument) {
    setBatchResults([]);
    setBatchSelection((current) => {
      if (current.some((item) => item.id === instrument.id)) {
        return current.filter((item) => item.id !== instrument.id);
      }
      if (current.length >= MAX_BATCH_INSTRUMENTS) {
        setError(`Select up to ${MAX_BATCH_INSTRUMENTS} instruments per research batch.`);
        return current;
      }
      return [...current, instrument];
    });
  }

  async function runResearchBatch(instrumentsToRun: Instrument[]) {
    if (instrumentsToRun.length === 0) return;
    setBusy(true);
    setError('');
    setNotice('');
    setBatchResults([]);
    let successCount = 0;
    const syncWarnings: string[] = [];
    for (const instrument of instrumentsToRun.slice(0, MAX_BATCH_INSTRUMENTS)) {
      try {
        const clientProgress: ResearchProgressEvent[] = [];
        if (!instrument.is_synthetic) {
          clientProgress.push({ component: 'market_data_sync', state: 'running', detail: 'Checking missing daily bars through the existing provider-priority and history-sync path.', updated_at: new Date().toISOString() });
          setBatchResults((current) => [...current.filter((item) => item.instrument.id !== instrument.id), { instrument, brief: null, error: null, status: 'preparing', progress: [...clientProgress] }]);
          const now = new Date();
          const start = new Date(now);
          start.setUTCFullYear(start.getUTCFullYear() - 2);
          const syncQuery = new URLSearchParams({
            start: start.toISOString(),
            end: now.toISOString(),
            interval: '1d',
            limit: '500',
          });
          const historyResult = await Promise.resolve().then(() => apiRequest<{ bars: unknown[]; provider_attempts?: { provider: string; outcome: string }[] }>(
            `/market-data/instruments/${instrument.id}/history/sync?${syncQuery}`,
            { method: 'POST' },
          )).then((value) => ({ status: 'fulfilled' as const, value }), (reason: unknown) => ({ status: 'rejected' as const, reason }));
          if (historyResult.status === 'rejected') {
            syncWarnings.push(`${instrument.symbol}: historical data sync failed.`);
            clientProgress[0] = { ...clientProgress[0], state: 'failed', detail: 'Existing history-sync request failed; stored bars remain available for the report.' };
          } else if (historyResult.value.bars.length === 0) {
            syncWarnings.push(`${instrument.symbol}: no real daily bars were returned.`);
            clientProgress[0] = { ...clientProgress[0], state: 'unavailable', detail: 'History sync returned no daily bars; no values will be fabricated.' };
          } else {
            clientProgress[0] = { ...clientProgress[0], state: 'checked', detail: `${historyResult.value.bars.length} stored or refreshed daily bars returned by the configured provider chain.` };
          }
        }
        let run = await apiRequest<ResearchRunState>(`/research/instruments/${instrument.id}/run`, {
          method: 'POST',
          body: '{}',
        });
        const updateRun = (value: ResearchRunState) => {
          setBatchResults((current) => [...current.filter((item) => item.instrument.id !== instrument.id), {
            instrument, brief: value.brief, error: null, status: value.status,
            run_id: value.run_id, progress: value.progress,
          }]);
        };
        updateRun(run);
        while (run.status === 'running') {
          await new Promise((resolve) => window.setTimeout(resolve, 1000));
          run = await apiRequest<ResearchRunState>(`/research/runs/${run.run_id}`);
          updateRun(run);
        }
        if (run.brief) {
          successCount += 1;
          if (selected?.id === instrument.id) {
            setBrief(run.brief);
            try {
              await refreshNotesAndSources(instrument.id);
            } catch (refreshError) {
              setError(refreshError instanceof Error ? refreshError.message : 'Research sources saved, but the page could not refresh its source list.');
            }
          }
        } else if (run.status === 'failed' || run.status === 'cancelled') {
          syncWarnings.push(`${instrument.symbol}: research ${run.status}.`);
        }
      } catch (requestError) {
        const message = requestError instanceof Error ? requestError.message : 'Research run failed.';
        setBatchResults((current) => [...current.filter((item) => item.instrument.id !== instrument.id), { instrument, brief: null, error: message, status: 'failed' }]);
      }
    }
    const failureCount = instrumentsToRun.length - successCount;
    const runNotice = failureCount
      ? `Research finished for ${successCount} of ${instrumentsToRun.length} instruments. Failed instruments are listed below.`
      : `Research finished for all ${successCount} selected instrument(s).`;
    const uniqueSyncWarnings = [...new Set(syncWarnings)];
    setNotice(uniqueSyncWarnings.length
      ? `${runNotice} Data sync notes: ${uniqueSyncWarnings.join(' ')}`
      : runNotice);
    setBusy(false);
  }

  async function cancelResearchRun(runId: string) {
    try {
      const state = await apiRequest<ResearchRunState>(`/research/runs/${runId}/cancel`, { method: 'POST', body: '{}' });
      setBatchResults((current) => current.map((item) => item.run_id === runId
        ? { ...item, status: state.status, progress: state.progress }
        : item));
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : 'Research cancellation failed.');
    }
  }

  async function addSourceLink(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selected) return;
    setBusy(true);
    setError('');
    setNotice('');
    try {
      await apiRequest<SourceLink>(`/research/instruments/${selected.id}/source-links`, {
        method: 'POST',
        body: JSON.stringify({ url: sourceUrl.trim(), label: sourceLabel.trim() }),
      });
      setSourceUrl('');
      setSourceLabel('');
      setNotice('Custom source saved for this instrument.');
      await refreshNotesAndSources(selected.id);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : 'Could not save the source URL.');
    } finally {
      setBusy(false);
    }
  }

  async function toggleSource(link: SourceLink) {
    if (!selected) return;
    try {
      await apiRequest<SourceLink>(`/research/source-links/${link.id}`, {
        method: 'PATCH',
        body: JSON.stringify({ enabled: !link.enabled }),
      });
      await refreshNotesAndSources(selected.id);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : 'Could not update the source.');
    }
  }

  async function deleteSourceLink(link: SourceLink) {
    if (!selected || !window.confirm(`Remove ${link.label} from this instrument's sources?`)) return;
    try {
      await apiRequest<void>(`/research/source-links/${link.id}`, { method: 'DELETE' });
      await refreshNotesAndSources(selected.id);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : 'Could not remove the source.');
    }
  }

  async function addLegacySource(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selected) return;
    setBusy(true);
    setError('');
    try {
      const result = await apiRequest<Source>('/research/sources', {
        method: 'POST',
        body: JSON.stringify({ url: legacySourceUrl.trim(), instrument_id: selected.id }),
      });
      setLegacySourceUrl('');
      setNotice(result.deduplicated ? 'Matching saved source reused.' : 'Source fetched and saved.');
      await refreshNotesAndSources(selected.id);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : 'Could not fetch source.');
    } finally {
      setBusy(false);
    }
  }

  function startNote(note?: Note) {
    if (!note) {
      setEditingNote(null);
      setNoteAsOf(today());
      setNoteSummary('');
      setNoteConfidence('medium');
      setNoteFacts([{ statement: '', as_of: today(), source_ids: [] }]);
      return;
    }
    setEditingNote(note.id);
    setNoteAsOf(note.as_of);
    setNoteSummary(note.summary);
    setNoteConfidence(note.confidence);
    setNoteFacts(note.key_facts.length > 0
      ? note.key_facts.map((fact) => ({
        statement: fact.statement,
        as_of: fact.as_of,
        source_ids: fact.source_ids,
      }))
      : [{ statement: '', as_of: note.as_of, source_ids: note.source_ids }]);
  }

  async function saveNote(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selected || !noteFacts.length || noteFacts.some((fact) => !fact.statement.trim() || !fact.source_ids.length)) {
      setError('Add at least one fact and cite a saved source for each fact.');
      return;
    }
    setBusy(true);
    setError('');
    try {
      const body = {
        as_of: noteAsOf,
        summary: noteSummary.trim(),
        confidence: noteConfidence,
        key_facts: noteFacts.map((fact) => ({
          statement: fact.statement.trim(),
          as_of: fact.as_of,
          source_ids: fact.source_ids,
        })),
      };
      await apiRequest<Note>(
        editingNote ? `/research/notes/${editingNote}` : '/research/notes',
        {
          method: editingNote ? 'PATCH' : 'POST',
          body: JSON.stringify(editingNote ? body : { ...body, instrument_id: selected.id }),
        },
      );
      startNote();
      setNotice(editingNote ? 'My note updated.' : 'My note saved.');
      await refreshNotesAndSources(selected.id);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : 'Could not save the note.');
    } finally {
      setBusy(false);
    }
  }

  async function deleteNote(note: Note) {
    if (!selected || !window.confirm('Delete this personal research note?')) return;
    try {
      await apiRequest<void>(`/research/notes/${note.id}`, { method: 'DELETE' });
      await refreshNotesAndSources(selected.id);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : 'Could not delete the note.');
    }
  }

  const watched = (instrument: Instrument) =>
    watchlist.some((item) => item.instrument_id === instrument.id);
  const sourceLabelFor = (id: string) =>
    legacySources.find((source) => source.id === id)?.title
    || legacySources.find((source) => source.id === id)?.publisher
    || legacySources.find((source) => source.id === id)?.url
    || 'Saved source';
  const visibleInstrumentSentiments = watchlist.length
    ? instrumentSentiments.filter((item) => watchlist.some((entry) => entry.instrument_id === item.instrument?.id))
    : instrumentSentiments.slice(0, 12);
  const impactfulSentiment = recentSentiment.filter((item) => item.impact === 'high').slice(0, 8);
  const visibleRecentSentiment = impactfulSentiment.length ? impactfulSentiment : recentSentiment.slice(0, 6);

  return (
    <div className="backfill-page research-pipeline-page">
      <p className="decision-disclaimer">Decision support, not investment advice. The template brief uses stored data and cited sources; it does not use AI to calculate or invent figures.</p>
      {error && <p className="form-error" role="alert">{error}</p>}
      {notice && <p className="alerts-notice" role="status">{notice}</p>}

      <details className="research-market-intel">
        <summary>Market news & sentiment · {storedNews.length} persisted articles · {marketSentiment?.event_count ?? 0} analyzed events</summary>
      <section className="data-section research-brief-section" aria-label="Persisted market news">
        <div className="section-heading"><div><p className="eyebrow">MARKET INTEL</p><h2>Persisted news</h2></div><span className="section-count">{storedNews.length}</span></div>
        {storedNewsError && <p className="research-status-line">News feed unavailable: {storedNewsError}</p>}
        {newsHealth.length > 0 && <div className="research-value-grid" aria-label="News source health">{newsHealth.map((source) => <span key={source.source}>
          <small>{source.source} · {source.status}{source.rate_limited ? ' · rate limited' : ''}</small>
          <strong>{source.articles_discovered} discovered · {source.articles_inserted} inserted · {source.duplicate_count} duplicates</strong>
          <small>Attempt {new Date(source.last_attempted_at).toLocaleString()} · last success {source.last_successful_poll_at ? new Date(source.last_successful_poll_at).toLocaleString() : 'none'} · last article {source.last_successful_article_at ? new Date(source.last_successful_article_at).toLocaleString() : 'none'} · failures {source.failure_count}{source.last_error_category ? ` · ${source.last_error_category}` : ''}</small>
        </span>)}</div>}
        {storedNews.length === 0 && !storedNewsError && <p className="research-status-line">No persisted news items are available yet.</p>}
        {storedNews.map((item) => <article className="backfill-card research-news-item" key={`${item.source}-${item.external_id}`}>
          <strong>{item.headline}</strong>
          <span>{item.publisher ?? item.source} · {new Intl.DateTimeFormat('en-IN', { dateStyle: 'medium', timeStyle: 'short', timeZone: 'Asia/Kolkata' }).format(new Date(item.published_at))} IST{item.published_time_kind === 'gdelt_seen_at' ? ' · GDELT first seen' : ''}</span>
          <span>{item.symbol_raw ?? 'Market-wide'} · {item.category ?? 'Uncategorized'} · News sentiment unavailable</span>
          {item.body && <p>{item.body} <small>(feed description; not full article text)</small></p>}
          <small>Full text: {item.full_text_status === 'available' ? 'available' : 'not stored'}</small>
          {item.source_url && <a href={item.source_url} target="_blank" rel="noreferrer">Open original article</a>}
        </article>)}
      </section>

      <section className="data-section research-brief-section" aria-label="News sentiment analysis">
        <div className="section-heading"><div><p className="eyebrow">NEWS SENTIMENT</p><h2>Evidence-linked sentiment</h2></div>
          <button className="secondary-button" type="button" disabled={sentimentBusy} onClick={() => void runSentimentAnalysis()}>{sentimentBusy ? 'Analyzing persisted news…' : 'Analyze next 500 news items'}</button>
        </div>
        {sentimentError && <p className="form-error" role="alert">{sentimentError}</p>}
        <p className="research-status-line">Local VADER plus financial event/context rules. Uses persisted headlines and permitted summaries only; no paid service is called.</p>
        <div className="research-value-grid">
          <span><small>Indian market · last 7 days · {marketSentiment?.status ?? 'loading'}</small>
            <strong>{marketSentiment?.sentiment ? `${marketSentiment.sentiment.toUpperCase()} · ${scoreLabel(marketSentiment.score)}` : 'No sentiment score'}</strong>
            <small>{sentimentStatusText(marketSentiment)}</small>
            <small>Confidence {marketSentiment?.confidence_score ?? '—'}/100 · {marketSentiment?.event_count ?? 0} unique events · {marketSentiment?.article_count ?? 0} articles</small>
            {marketSentiment?.freshness.latest_published_at && <small>Latest analyzed publication {new Date(marketSentiment.freshness.latest_published_at).toLocaleString('en-IN', { timeZone: 'Asia/Kolkata' })} IST</small>}
          </span>
          <span><small>Unique-event distribution</small><strong>Positive {marketSentiment?.counts.positive ?? 0} · Negative {marketSentiment?.counts.negative ?? 0}</strong>
            <small>Neutral {marketSentiment?.counts.neutral ?? 0} · Mixed {marketSentiment?.counts.mixed ?? 0}</small><small>Counts are event groups, not duplicated publisher articles.</small>
            <small>{marketSentiment?.coverage.analysis.pending_items ?? 0} pending · {marketSentiment?.coverage.analysis.failed_items ?? 0} failed · {marketSentiment?.coverage.analysis.analyzed_items ?? 0} analyzed for this classifier version.</small></span>
        </div>
        <div className="research-value-grid">
          <span><small>Individual stock sentiment · mapped instruments</small>
            {visibleInstrumentSentiments.length === 0 ? <strong>No analyzed company news is mapped to this watchlist yet.</strong> : visibleInstrumentSentiments.slice(0, 12).map((item) => <small key={item.scope_id}><strong>{item.instrument?.symbol ?? item.scope_id}:</strong> {item.sentiment ?? item.status} · {scoreLabel(item.score)} · confidence {item.confidence_score ?? '—'}/100</small>)}
          </span>
          <span><small>Sector sentiment · persisted sector mappings only</small>
            {sectorSentiments.length === 0 ? <strong>Sector sentiment unavailable; no saved sector mappings are available.</strong> : sectorSentiments.slice(0, 12).map((item) => <small key={item.scope_id}><strong>{item.scope_id}:</strong> {item.sentiment ?? item.status} · {scoreLabel(item.score)} · {item.event_count} events</small>)}
          </span>
        </div>
        <h3>Recent impactful news</h3>
        {visibleRecentSentiment.length === 0 && <p className="research-status-line">{marketSentiment?.status === 'not_analyzed' ? 'News has not yet been analyzed.' : marketSentiment?.status === 'source_unavailable' ? 'News source unavailable; no recent analyzed events.' : 'No analyzed recent events are available.'}</p>}
        {visibleRecentSentiment.map((item) => <article className="backfill-card research-news-item" key={item.news_item_id}>
          <strong>{item.headline}</strong><span>{item.interpretation === 'uncertain' ? `UNCERTAIN · model label ${item.sentiment.toUpperCase()}` : item.sentiment.toUpperCase()} · {item.event_category.replaceAll('_', ' ')} · impact {item.impact} · confidence {item.confidence_score}/100 · {item.symbol_raw ?? (item.instrument_id ? 'Mapped company' : item.relevance_scope)}</span>
          <span>{item.publisher ?? item.source} · {new Intl.DateTimeFormat('en-IN', { dateStyle: 'medium', timeStyle: 'short', timeZone: 'Asia/Kolkata' }).format(new Date(item.published_at))} IST</span>
          <small>{item.explanation} Evidence: {item.evidence.map((evidence) => evidence.text).join(' · ')}</small>
          {item.summary && <p>{item.summary} <small>(available feed summary)</small></p>}{item.url && <a href={item.url} target="_blank" rel="noreferrer">Open source article</a>}
        </article>)}
        {marketSentiment?.coverage.sources.map((source) => <small className="research-source-citation" key={`sentiment-${source.source}`}>{source.source}: {source.status}{source.rate_limited ? ' · rate limited' : ''}{source.last_error_category ? ` · ${source.last_error_category}` : ''}</small>)}
      </section>
      </details>

      <section className="data-section research-search-panel">
        <div className="section-heading"><div><p className="eyebrow">INSTRUMENT RESEARCH</p><h2>Find an instrument</h2></div></div>
        <form className="market-search" onSubmit={searchInstruments}>
          <label>Symbol or company name<input value={instrumentQuery} onChange={(event) => setInstrumentQuery(event.target.value)} placeholder="AAPL, RELIANCE, company name" required /></label>
          <button className="primary-button" type="submit" disabled={searching}>{searching ? 'Searching…' : 'Search instruments'}</button>
        </form>
        {instruments.length > 0 && <div className="research-instrument-results">
          {instruments.map((instrument) => <article className="research-instrument-choice" key={instrument.id}>
            <div className="research-batch-choice">
              <label className="research-batch-checkbox">
                <input type="checkbox" checked={batchSelection.some((item) => item.id === instrument.id)} onChange={() => toggleBatchInstrument(instrument)} disabled={busy || (!batchSelection.some((item) => item.id === instrument.id) && batchSelection.length >= MAX_BATCH_INSTRUMENTS)} />
                <span>Add to batch</span>
              </label>
              <button className="market-instrument" type="button" onClick={() => { setBatchResults([]); setSelected(instrument); }}>
                <span><strong>{instrument.symbol}</strong><small>{instrument.display_name}</small></span>
                <span className="market-instrument-meta">{instrument.venue}</span>
              </button>
              <button className="secondary-button" type="button" onClick={() => void (watched(instrument) ? removeInstrumentFromWatchlist(instrument.id) : addInstrumentToWatchlist(instrument.id, instrument.symbol))}>
                {watched(instrument) ? 'Remove from watchlist' : 'Add to watchlist'}
              </button>
            </div>
          </article>)}
          <div className="research-batch-actions">
            <span>{batchSelection.length}/{MAX_BATCH_INSTRUMENTS} selected</span>
            <button className="primary-button" type="button" onClick={() => void runResearchBatch(batchSelection)} disabled={busy || batchSelection.length === 0}>{busy ? 'Running selected research…' : `Run research for ${batchSelection.length} selected`}</button>
          </div>
        </div>}
      </section>

      {batchResults.length > 0 && <section className="research-batch-results">
        <div className="section-heading"><div><p className="eyebrow">BATCH RESULTS</p><h2>Research runs</h2></div><span className="section-count">{batchResults.length}</span></div>
        {batchResults.map((item) => <details className="data-section research-batch-result" key={item.instrument.id}>
          <summary><strong>{item.instrument.symbol} · {item.instrument.display_name}</strong><span className={item.error ? 'research-run-status research-status-failed' : `research-run-status research-status-${item.status ?? item.brief?.status ?? 'preparing'}`}>{item.error ? 'request failed' : item.status ?? item.brief?.status ?? 'preparing'}</span></summary>
          {item.error ? <p className="form-error" role="alert">{item.error}</p> : <>
            {item.status === 'running' && item.run_id && <button className="text-button" type="button" onClick={() => void cancelResearchRun(item.run_id!)}>Cancel this research run</button>}
            {item.progress && <div className="research-section-status-grid" aria-label={`${item.instrument.symbol} research progress`}>
              {item.progress.slice(-8).map((step, index) => <article key={`${step.component}-${step.updated_at}-${index}`}>
                <small>{step.component.replaceAll('_', ' ')}</small><strong>{step.state}</strong><span>{step.detail}</span>
                {step.source && <small>{step.source}{step.retrieved_at ? ` · ${new Date(step.retrieved_at).toLocaleString()}` : ''}</small>}
              </article>)}
            </div>}
            {item.brief && item.instrument.id !== selected?.id && <ResearchBriefView brief={item.brief} instrument={item.instrument} />}
          </>}
        </details>)}
      </section>}

      {selected && <>
        <section className="data-section research-selected-panel">
          <div className="section-heading">
            <div><p className="eyebrow">SELECTED INSTRUMENT</p><h2>{selected.symbol} · {selected.display_name}</h2><p className="research-subtitle">{selected.venue} · {selected.is_synthetic ? 'Synthetic instrument' : 'NSE/BSE equity'}</p></div>
            <div className="research-action-bar" aria-label="Selected stock research actions">
              {!selected.is_synthetic && <button className="secondary-button" type="button" onClick={() => void refreshSelectedData()} disabled={busy}>{busy ? 'Working…' : 'Refresh data'}</button>}
              <button className="primary-button" type="button" onClick={() => void runResearch()} disabled={busy}>{busy ? 'Researching…' : 'Run research'}</button>
              {brief && <button className="text-button" type="button" onClick={viewPreviousReport}>Previous report · {new Date(brief.created_at).toLocaleDateString()}</button>}
            </div>
          </div>
          <p className="research-subtitle">Research reuses stored fundamentals and OHLCV, refreshes missing daily bars through the configured provider path, and preserves the last completed report if a new run fails.</p>
        </section>

        <section className="data-section research-brief-section" aria-label="Selected instrument news sentiment">
          <h3>{selected.symbol} sentiment · {selectedSentiment?.status ?? 'loading'}</h3>
          <p className="research-status-line">{sentimentStatusText(selectedSentiment)}</p>
          {selectedSentiment?.sentiment && <p><strong>{selectedSentiment.sentiment.toUpperCase()}</strong> · score {scoreLabel(selectedSentiment.score)} · confidence {selectedSentiment.confidence_score}/100 · {selectedSentiment.event_count} events · contradictory evidence {selectedSentiment.contradictory_evidence ? 'present' : 'not detected'}</p>}
          {selectedSentiment?.events.slice(0, 5).map((event) => <article className="backfill-card research-news-item" key={event.event_key}>
            <strong>{event.sentiment.toUpperCase()} · {event.event_category.replaceAll('_', ' ')} · {event.impact} impact</strong>
            {event.items.map((item) => <div key={item.news_item_id}><span>{item.publisher ?? item.source} · {item.headline}</span>{item.url && <a href={item.url} target="_blank" rel="noreferrer">Source</a>}</div>)}
          </article>)}
          <h4>Historical sentiment</h4>
          {selectedSentimentHistory.length < 3 ? <p className="research-status-line">Not enough analyzed history to show a trend.</p> : selectedSentimentHistory.slice(-30).map((point) => <small className="research-source-citation" key={point.date}>{point.date}: {scoreLabel(point.score)} · {point.event_count} events</small>)}
        </section>

        <section className="entry-section research-source-manager">
          <div className="section-heading"><div><p className="eyebrow">SOURCE REGISTRY</p><h2>Custom source URLs</h2></div><span className="section-count">{sourceLinks.length}</span></div>
          <form className="research-source-form" onSubmit={addSourceLink}>
            <label>HTTPS source URL<input type="url" value={sourceUrl} onChange={(event) => setSourceUrl(event.target.value)} placeholder="https://company.example/investors" required /></label>
            <label>Label<input value={sourceLabel} onChange={(event) => setSourceLabel(event.target.value)} maxLength={200} placeholder="Investor relations" required /></label>
            <button className="secondary-button" type="submit" disabled={busy}>Add source</button>
          </form>
          {sourceLinks.length === 0 ? <p className="table-empty">No custom sources. Add investor-relations or other trusted URLs.</p> : sourceLinks.map((link) => (
            <article className="research-link-row" key={link.id}>
              <div><strong>{link.label}</strong><a href={link.url} target="_blank" rel="noreferrer">{link.url}</a></div>
              <div className="backfill-actions">
                <button className="secondary-button" type="button" onClick={() => void toggleSource(link)}>{link.enabled ? 'Disable' : 'Enable'}</button>
                <button className="text-button" type="button" onClick={() => void deleteSourceLink(link)}>Remove</button>
              </div>
            </article>
          ))}
        </section>

        {brief && <ResearchBriefView brief={brief} instrument={selected} />}

        <details className="data-section my-notes-section">
          <summary><span><span className="eyebrow">OPTIONAL</span><strong>My notes</strong></span><small>Personal, source-cited notes</small></summary>
          <div className="my-notes-content">
            <form className="form-stack" onSubmit={addLegacySource}>
              <label>Add a source for my notes<input type="url" value={legacySourceUrl} onChange={(event) => setLegacySourceUrl(event.target.value)} placeholder="https://…" required /></label>
              <button className="secondary-button" type="submit" disabled={busy}>{busy ? 'Fetching…' : 'Fetch source'}</button>
            </form>
            {legacySources.map((source) => <article className="backfill-card" key={source.id}>
              <strong>{source.title || source.publisher || source.url}</strong>
              <a href={source.url} target="_blank" rel="noreferrer">{source.url}</a>
              <span>{source.publisher || 'Publisher not identified'} · fetched {new Date(source.fetched_at).toLocaleString()}</span>
            </article>)}
            <form className="form-stack research-note-form" onSubmit={saveNote}>
              <div className="section-heading"><h3>{editingNote ? 'Edit personal note' : 'Add personal note'}</h3></div>
              <label>As of<input type="date" value={noteAsOf} onChange={(event) => setNoteAsOf(event.target.value)} required /></label>
              <label>Summary<textarea value={noteSummary} onChange={(event) => setNoteSummary(event.target.value)} maxLength={4000} rows={3} required /></label>
              <label>Confidence<select value={noteConfidence} onChange={(event) => setNoteConfidence(event.target.value as Note['confidence'])}><option value="low">Low</option><option value="medium">Medium</option><option value="high">High</option></select></label>
              <fieldset>
                <legend>Each fact requires a source and an as-of date</legend>
                {noteFacts.map((fact, index) => <div className="research-fact" key={index}>
                  <label>Fact<textarea value={fact.statement} onChange={(event) => setNoteFacts(noteFacts.map((item, i) => i === index ? { ...item, statement: event.target.value } : item))} maxLength={1000} required /></label>
                  <label>Fact as of<input type="date" value={fact.as_of} onChange={(event) => setNoteFacts(noteFacts.map((item, i) => i === index ? { ...item, as_of: event.target.value } : item))} required /></label>
                  <label>Cited sources<select multiple value={fact.source_ids} onChange={(event) => setNoteFacts(noteFacts.map((item, i) => i === index ? { ...item, source_ids: Array.from(event.target.selectedOptions, (option) => option.value) } : item))} required>{legacySources.map((source) => <option key={source.id} value={source.id}>{source.title || source.publisher || source.url}</option>)}</select></label>
                  {noteFacts.length > 1 && <button className="text-button" type="button" onClick={() => setNoteFacts(noteFacts.filter((_, i) => i !== index))}>Remove fact</button>}
                </div>)}
                <button className="secondary-button" type="button" onClick={() => setNoteFacts([...noteFacts, { statement: '', as_of: today(), source_ids: [] }])}>Add fact</button>
              </fieldset>
              <div className="backfill-actions"><button className="primary-button" type="submit" disabled={busy || legacySources.length === 0}>{editingNote ? 'Save note' : 'Create note'}</button>{editingNote && <button className="secondary-button" type="button" onClick={() => startNote()}>Cancel</button>}</div>
            </form>
            {notes.map((note) => <article className="backfill-card research-personal-note" key={note.id}>
              <header><strong>{note.as_of}</strong>{note.stale && <span className="stale-marker">STALE</span>}</header>
              <p>{note.summary}</p>
              <ul>{note.key_facts.map((fact, index) => <li key={`${fact.as_of}-${index}`}>{fact.statement} · {fact.source_ids.map(sourceLabelFor).join(', ')}{fact.stale ? ' · stale' : ''}</li>)}</ul>
              <div className="backfill-actions"><button className="secondary-button" type="button" onClick={() => startNote(note)}>Edit</button><button className="text-button" type="button" onClick={() => void deleteNote(note)}>Delete</button></div>
            </article>)}
            {notes.length === 0 && <p className="table-empty">No personal notes saved for this instrument.</p>}
          </div>
        </details>
      </>}
    </div>
  );
}

export function ResearchBriefView({ brief, instrument }: { brief: Brief; instrument?: Instrument | null }) {
  const { fundamentals, technicals, financials_trend: trend, news, news_sentiment: sentiment, collector_status: collectors, data_quality: quality } = brief.sections;
  const qualityGroups = new Map<string, QualityItem>();
  quality.items.forEach((item) => qualityGroups.set(`${item.kind}|${item.reason}`, item));
  const groupedQuality = Array.from(qualityGroups.values()).map((item) => ({
    ...item,
    count: quality.items.filter((candidate) =>
      candidate.kind === item.kind && candidate.reason === item.reason
    ).length,
  }));
  const provenance = new Map<string, string>();
  Object.entries(fundamentals.snapshot.values ?? {}).forEach(([name, item]) => {
    provenance.set(item.field_path, `Snapshot · ${name.replaceAll('_', ' ')}`);
  });
  fundamentals.periods.forEach((period) => Object.entries(period.values).forEach(([name, item]) => {
    provenance.set(item.field_path, `${period.period_type} ${period.period_end} · ${name.replaceAll('_', ' ')}`);
  }));
  Object.entries(technicals.indicators).forEach(([name, item]) => {
    provenance.set(item.field_path, `Daily indicator · ${name.replaceAll('_', ' ')}`);
  });
  trend.periods.forEach((period) => Object.entries(period.values).forEach(([name, item]) => {
    item.field_paths.forEach((path) => provenance.set(path, `${period.period_type} trend · ${name.replaceAll('_', ' ')}`));
  }));
  quality.items.forEach((item) => provenance.set(item.field_path, item.reason));
  const displayValue = (value: string | null) => value === null
    ? 'Unavailable'
    : Number(value).toLocaleString('en-IN', { maximumFractionDigits: 4 });
  const intelligence = [
    ['Fundamental', fundamentals.status, fundamentals.status === 'available' ? `${fundamentals.periods.length} stored statement periods` : 'Insufficient stored evidence'],
    ['Financial trends', trend.status, trend.periods.length ? `${trend.periods.length} comparable periods` : 'Comparable history unavailable'],
    ['Technical', technicals.status, `${technicals.bars_available} daily bars · ${technicals.source ?? 'source unavailable'}`],
    ['News & sentiment', sentiment?.status ?? news.status, sentiment?.sentiment ? `${sentiment.sentiment} · ${sentiment.event_count} events` : 'No validated sentiment score'],
  ];
  const horizons = ['Intraday', 'Swing', 'Positional', 'Long-term'];
  return <section className="data-section research-brief" id="research-report">
    <header className="research-report-header">
      <div><p className="eyebrow">ALGORITHMIC RESEARCH REPORT</p>
        <h2>{instrument ? `${instrument.symbol} · ${instrument.display_name}` : 'Stock research report'}</h2>
        <p>{instrument?.venue ?? 'Exchange unavailable'} · As of {new Date(brief.created_at).toLocaleString()}</p>
      </div>
      <span className={`research-run-status research-status-${brief.status}`}>{brief.status === 'partial' ? 'Evidence incomplete' : brief.status}</span>
    </header>
    <section className="research-overall-assessment">
      <div><p className="eyebrow">OVERALL ALGORITHMIC ASSESSMENT</p><h3>Not yet scored</h3></div>
      <p>The unified decision engine and recommendation logic are not implemented. This report presents available evidence without inventing a rating or investment recommendation.</p>
    </section>
    <section className="research-intelligence-grid" aria-label="Independent intelligence components">
      {intelligence.map(([name, status, evidence]) => <article key={name}>
        <small>{name} intelligence</small><strong>{status}</strong><span>Score unavailable</span><p>{evidence}</p>
      </article>)}
    </section>
    <section className="research-horizon-grid" aria-label="Assessment horizons">
      {horizons.map((horizon) => <article key={horizon}><small>{horizon}</small><strong>Not assessed</strong><span>Horizon-specific engine not implemented</span></article>)}
    </section>
    <p className="research-subtitle">Evidence snapshot generated {new Date(brief.created_at).toLocaleString()}. Technical levels, valuation comparisons, scenarios, risk gates and recommendations are not produced unless supported by implemented components and sufficient data.</p>
    <nav className="research-report-nav" aria-label="Research report sections">
      <a href="#research-report">Overview</a>
      <a href="#research-fundamentals">Fundamentals</a>
      <a href="#research-financial-statements">Financial statements</a>
      <a href="#research-technicals">Technical analysis</a>
      <a href="#research-news">News</a>
      {sentiment && <a href="#research-sentiment">Sentiment</a>}
    </nav>
    <details className="research-diagnostics">
      <summary>Research run diagnostics · {brief.status} · {brief.failures.length} source/run failures</summary>
    <div className="research-section-status-grid">
      {[
        ['Fundamentals', fundamentals.status, fundamentals.status === 'available' ? `${fundamentals.periods.length} stored periods` : 'No non-null provider values'],
        ['Technicals', technicals.status, technicals.message ?? `${technicals.bars_available} stored daily bars · ${technicals.source ?? 'source unavailable'}`],
        ['News', news.status, news.message ?? `${news.items.length} cited headlines`],
      ].map(([title, status, detail]) => <article key={title}>
        <small>{title}</small><strong>{status}</strong><span>{detail}</span>
      </article>)}
    </div>
    {brief.failures.length > 0 && <section className="research-failures" aria-label="Source failures">
      <h3>Sources that failed</h3>
      {brief.failures.map((failure, index) => <article key={`${failure.source}-${index}`}>
        <strong>{failure.source}</strong><span>{sourceFailureSummary(failure)}</span>
        <details><summary>Fetch details</summary><small>{failure.stage} · {failure.reason}</small><a href={failure.url} target="_blank" rel="noreferrer">{failure.url}</a></details>
      </article>)}
    </section>}
    </details>

    <section className="research-brief-section" id="research-fundamentals">
      <h3>Fundamentals</h3>
      <p className="research-status-line">{fundamentals.status === 'unavailable' ? 'Unavailable from stored providers.' : 'Stored M5.3 fundamentals; values are not estimated.'}</p>
      {fundamentals.snapshot.status === 'available' && fundamentals.snapshot.values && <>
        {fundamentals.snapshot.conflicts && fundamentals.snapshot.conflicts.length > 0 && <p className="form-error">Conflicting provider values were retained separately; no value was averaged or silently reconciled. {fundamentals.snapshot.conflicts.map((conflict) => `${conflict.field}: ${conflict.source_a} ${conflict.value_a} vs ${conflict.source_b} ${conflict.value_b}`).join(' · ')}</p>}
        <div className="research-snapshot-grid">{Object.entries(fundamentals.snapshot.values).map(([name, item]) => <article key={name}>
          <small>{name.replaceAll('_', ' ')}</small><strong>{displayValue(name === 'shares_outstanding' && item.value !== null && Number(item.value) === 0 ? null : item.value)}</strong>
        </article>)}</div>
        <p className="research-subtitle">Source: {fundamentals.snapshot.source} · fetched {new Date(fundamentals.snapshot.fetched_at ?? '').toLocaleString()} {fundamentals.snapshot.stale && <span className="stale-marker">STALE</span>}</p>
      </>}
      {fundamentals.periods.length > 0 && <div className="research-period-list">
        {fundamentals.periods.map((period) => <article className="research-period-card" key={`${period.period_type}-${period.period_end}`}>
          <header><strong>{period.period_type} · {period.period_end}</strong><span>{period.currency} · {period.source}{period.stale && <span className="stale-marker">STALE</span>}</span></header>
          <div className="research-value-grid">{Object.entries(period.values).map(([name, item]) => <span key={name}><small>{name.replaceAll('_', ' ')}</small><strong>{displayValue(name === 'shares_outstanding' && item.value !== null && Number(item.value) === 0 ? null : item.value)}</strong></span>)}</div>
          <small>Fetched {new Date(period.fetched_at).toLocaleString()}</small>
        </article>)}
      </div>}
    </section>

    <section className="research-brief-section" id="research-technicals">
      <h3>Technicals · stored daily OHLCV</h3>
      {technicals.message && <p className="research-status-line">{technicals.message}</p>}
      <div className="research-value-grid">{Object.entries(technicals.indicators).map(([name, item]) => <span key={name}>
        <small>{name.replaceAll('_', ' ')}</small>
        <strong>{item.value === null ? 'Insufficient history' : displayValue(item.value)}</strong>
        <small>{item.bars_used}/{item.bars_required} bars{item.is_synthetic ? ' · synthetic' : ''}</small>
      </span>)}</div>
      {technicals.source && <small>Source {technicals.source} · latest bar {technicals.latest_bar ? new Date(technicals.latest_bar).toLocaleString() : 'unavailable'}</small>}
    </section>

    <section className="research-brief-section" id="research-financial-statements">
      <h3>Financials trend · year-on-year from stored periods</h3>
      {trend.status === 'unavailable' && <p className="research-status-line">Unavailable: no comparable stored periods.</p>}
      {trend.periods.map((period) => <article className="research-period-card" key={`${period.period_type}-${period.period_end}`}>
        <header><strong>{period.period_type} · {period.period_end}</strong><span>{period.currency}</span></header>
        <div className="research-value-grid">{Object.entries(period.values).map(([name, item]) => <span key={name}>
          <small>{name.replaceAll('_', ' ')}</small><strong>{displayValue(item.value)}</strong>
          <small>YoY {item.year_on_year_growth_pct === null ? 'Unavailable' : `${displayValue(item.year_on_year_growth_pct)}%`}</small>
        </span>)}</div>
      </article>)}
    </section>

    <section className="research-brief-section" id="research-news">
      <h3>News and source citations</h3>
      {news.status === 'unavailable' && <p className="research-status-line">{news.message ?? 'No permitted configured news feeds returned parseable headlines. Add an issuer investor-relations or exchange feed URL.'}</p>}
      {news.items.map((item, index) => {
        const citation = news.sources.find((source) => source.source_id === item.source_id);
        return <article className="backfill-card research-news-item" key={`${item.source_id}-${index}`}>
          <strong>{item.headline}</strong>
          <a href={item.url} target="_blank" rel="noreferrer">{item.url}</a>
          <span>{item.publisher} · {item.published_at ? new Date(item.published_at).toLocaleString() : 'publication time unavailable'}</span>
          {citation && <small>Source: {citation.publisher} · fetched {new Date(citation.fetched_at).toLocaleString()} · <a href={citation.url} target="_blank" rel="noreferrer">view cited feed</a></small>}
        </article>;
      })}
      {news.sources.map((source) => <article className="research-source-citation" key={source.source_id}>
        <strong>{source.title}</strong><a href={source.url} target="_blank" rel="noreferrer">{source.url}</a>
        <span>{source.publisher} · fetched {new Date(source.fetched_at).toLocaleString()}</span>
      </article>)}
    </section>

    {sentiment && <section className="research-brief-section" id="research-sentiment">
      <h3>News sentiment · provisional context</h3>
      <p className="research-status-line">{sentiment.status === 'available' || sentiment.status === 'stale'
        ? `${sentiment.sentiment?.toUpperCase() ?? 'UNCERTAIN'} · score ${scoreLabel(sentiment.score)} · confidence ${sentiment.confidence_score ?? '—'}/100 · ${sentiment.event_count} events from ${sentiment.article_count} articles.`
        : `Sentiment ${sentiment.status}; no score is substituted for missing data.`}</p>
      <small>Classifier {sentiment.classifier_version ?? 'unknown'} · validation {sentiment.validation_status ?? 'unknown'} · this heuristic context is not predictive validation.</small>
      {sentiment.events.slice(0, 5).map((event) => <article className="backfill-card research-news-item" key={event.event_key}>
        <strong>{event.sentiment.toUpperCase()} · {event.event_category.replaceAll('_', ' ')} · {event.impact} impact</strong>
        <small>Confidence {event.confidence_score}/100 · sources {event.sources.join(', ')} · {event.news_item_ids.length} linked item(s){event.contradictory_sources ? ' · contradictory reports' : ''}</small>
        {event.items.slice(0, 3).map((item) => <span key={item.news_item_id}>{item.headline} · {item.publisher ?? item.source}{item.url ? <> · <a href={item.url} target="_blank" rel="noreferrer">source</a></> : null}</span>)}
      </article>)}
    </section>}

    {collectors && <details className="research-provenance research-collector-details">
      <summary>Independent source coverage and collector status</summary>
      <div className="research-section-status-grid">{Object.entries(collectors).map(([name, item]) => <article key={name}>
        <small>{name.replaceAll('_', ' ')}</small><strong>{item.state}</strong>
        {item.records !== undefined && <span>{item.records} mapped record(s)</span>}
        {item.source_count !== undefined && <span>{item.source_count} enabled source(s) checked</span>}
        {item.detail && <span>{item.detail}</span>}
        {item.source && <span>Source: {item.source}{item.retrieved_at ? ` · ${new Date(item.retrieved_at).toLocaleString()}` : ''}</span>}
        {item.sources && <span>News sources: {item.sources.length ? item.sources.join(', ') : 'no analyzed items attributed'}</span>}
      </article>)}</div>
    </details>}

    <section className="research-brief-section research-quality-section">
      <h3>Data quality</h3>
      {groupedQuality.length === 0 ? <p>No stale, unavailable, failed-fetch, or synthetic items recorded.</p> : groupedQuality.map((item) => <article key={`${item.kind}-${item.reason}`}>
        <span className={`research-quality-kind research-quality-${item.kind}`}>{item.kind.replaceAll('_', ' ')}</span><strong>{item.reason}</strong>{item.count > 1 && <small>Repeated across {item.count} fields</small>}
      </article>)}
    </section>
    <details className="research-provenance">
      <summary>Technical provenance · database paths and provider evidence</summary>
      <ul>{Array.from(provenance, ([path, label]) => <li key={path}><strong>{label}</strong><code>{path}</code></li>)}</ul>
    </details>
    <p className="decision-disclaimer">{brief.sections.disclaimer}</p>
  </section>;
}
