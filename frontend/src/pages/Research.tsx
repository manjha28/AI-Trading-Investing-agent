import { useCallback, useEffect, useState, type FormEvent } from 'react';
import { ApiRequestError, apiRequest } from '../api';
import './research.css';
import type { Instrument, WatchlistEntry, SourceLink, Source, Fact, Note, FieldValue, FundamentalPeriod, IndicatorValue, NewsItem, StoredNewsItem, NewsSourceHealth, SentimentArticle, SentimentEvent, SentimentAggregate, SentimentHistoryPoint, NewsSource, QualityItem, Brief, FactDraft, ResearchProgressEvent, ResearchRunState, BatchRunResult, ComponentKey, HorizonKey, ScoreFactor, ScoreComponent, HorizonAssessment, Assessment } from './researchTypes';
import { ResearchBriefView, plainText } from './ResearchReport';

const today = () => new Date().toISOString().slice(0, 10);
const MAX_BATCH_INSTRUMENTS = 5;

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
  const [horizon, setHorizon] = useState<HorizonKey>('positional');
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

  const selectedResult = batchResults.find((item) => item.instrument.id === selected?.id);
  const lastRun = brief?.finished_at ?? brief?.created_at ?? null;
  const technicalFresh = brief?.sections.technicals.status === 'available';
  const fmtIst = (value: string) => new Intl.DateTimeFormat('en-IN', { dateStyle: 'medium', timeStyle: 'short', timeZone: 'Asia/Kolkata' }).format(new Date(value)) + ' IST';

  return (
    <div className="rt-root research-pipeline-page">
      <p className="rt-disclaimer-top">Decision support, not investment advice. Scores are calculated by the backend from stored data and are a provisional, unvalidated model.</p>
      {error && <p className="rt-alert rt-alert-error" role="alert">{error}</p>}
      {notice && <p className="rt-alert" role="status">{notice}</p>}

      <section className="rt-card rt-command" aria-label="Research command bar">
        <form className="rt-search" onSubmit={searchInstruments} role="search">
          <label className="rt-sr" htmlFor="rt-query">Search stock or instrument</label>
          <input id="rt-query" value={instrumentQuery} onChange={(event) => setInstrumentQuery(event.target.value)} placeholder="Search symbol or company, e.g. RELIANCE" autoComplete="off" required />
          <button className="rt-btn" type="submit" disabled={searching}>{searching ? 'Searching…' : 'Search'}</button>
        </form>
        {instruments.length > 0 && <div className="rt-results" role="listbox" aria-label="Instrument search results">
          {instruments.map((instrument) => {
            const inBatch = batchSelection.some((item) => item.id === instrument.id);
            return <div className={`rt-result${selected?.id === instrument.id ? ' active' : ''}`} key={instrument.id} role="option" aria-selected={selected?.id === instrument.id}>
              <label className="rt-check" title="Add to batch"><input type="checkbox" checked={inBatch} onChange={() => toggleBatchInstrument(instrument)} disabled={busy || (!inBatch && batchSelection.length >= MAX_BATCH_INSTRUMENTS)} /><span className="rt-sr">Add {instrument.symbol} to batch</span></label>
              <button className="rt-result-main" type="button" onClick={() => { setBatchResults([]); setSelected(instrument); setInstruments([]); }}>
                <strong>{instrument.symbol}</strong><span>{instrument.display_name}</span><small>{instrument.venue}</small>
              </button>
              <button className="rt-btn rt-ghost" type="button" onClick={() => void (watched(instrument) ? removeInstrumentFromWatchlist(instrument.id) : addInstrumentToWatchlist(instrument.id, instrument.symbol))}>
                {watched(instrument) ? 'Unwatch' : 'Watch'}
              </button>
            </div>;
          })}
          <div className="rt-batch-actions">
            <span>{batchSelection.length}/{MAX_BATCH_INSTRUMENTS} in batch</span>
            <button className="rt-btn" type="button" onClick={() => void runResearchBatch(batchSelection)} disabled={busy || batchSelection.length === 0}>{busy ? 'Running…' : `Run batch (${batchSelection.length})`}</button>
          </div>
        </div>}
        {batchSelection.length > 0 && instruments.length === 0 && <div className="rt-batch-actions">
          <span>Batch: {batchSelection.map((item) => item.symbol).join(', ')}</span>
          <button className="rt-btn" type="button" onClick={() => void runResearchBatch(batchSelection)} disabled={busy}>{busy ? 'Running…' : `Run batch (${batchSelection.length})`}</button>
        </div>}
        <div className="rt-command-row">
          <div className="rt-selected">
            {selected ? <><strong>{selected.display_name}</strong><span>{selected.symbol} · {selected.venue}{selected.is_synthetic ? ' · synthetic' : ''}</span></>
              : <span className="rt-muted">No instrument selected. Search above to begin.</span>}
          </div>
          <div className="rt-horizon-select" role="group" aria-label="Investment horizon">
            {(['intraday', 'swing', 'positional', 'long_term'] as const).map((key) => <button key={key} type="button" aria-pressed={horizon === key} className={horizon === key ? 'active' : ''} onClick={() => setHorizon(key)}>
              {key === 'long_term' ? 'Long-term' : key[0].toUpperCase() + key.slice(1)}</button>)}
          </div>
          <div className="rt-actions">
            {!selected?.is_synthetic && <button className="rt-btn rt-ghost" type="button" onClick={() => void refreshSelectedData()} disabled={busy || !selected}>{busy ? 'Working…' : 'Refresh market data'}</button>}
            <button className="rt-btn rt-primary" type="button" onClick={() => void runResearch()} disabled={busy || !selected}>{busy ? 'Researching…' : 'Run research'}</button>
          </div>
        </div>
        {selected && <div className="rt-status-row">
          <span>Last research: {lastRun ? fmtIst(lastRun) : 'never'}</span>
          {brief && <span className={`rt-chip ${technicalFresh ? 'rt-good' : 'rt-bad'}`}>{technicalFresh ? 'Price data current' : `Price data ${brief.sections.technicals.status.replaceAll('_', ' ')}`}</span>}
          {brief && <button className="rt-link" type="button" onClick={viewPreviousReport}>Jump to report</button>}
        </div>}
      </section>

      <details className="rt-card rt-market research-market-intel">
        <summary>Market-wide news &amp; source health (secondary) · {storedNews.length} persisted articles · {marketSentiment?.event_count ?? 0} analyzed events</summary>
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
          {item.body && <p>{plainText(item.body)} <small>(feed description; not full article text)</small></p>}
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
          {item.summary && <p>{plainText(item.summary)} <small>(available feed summary)</small></p>}{item.url && <a href={item.url} target="_blank" rel="noreferrer">Open source article</a>}
        </article>)}
        {marketSentiment?.coverage.sources.map((source) => <small className="research-source-citation" key={`sentiment-${source.source}`}>{source.source}: {source.status}{source.rate_limited ? ' · rate limited' : ''}{source.last_error_category ? ` · ${source.last_error_category}` : ''}</small>)}
      </section>
      </details>

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
            {item.brief && item.instrument.id !== selected?.id && <ResearchBriefView brief={item.brief} instrument={item.instrument} horizon={horizon} onHorizon={setHorizon} />}
          </>}
        </details>)}
      </section>}


      {selected && <>
        {selectedResult && selectedResult.status === 'running' && <section className="rt-card" aria-live="polite">
          <strong>Research running…</strong>
          {selectedResult.run_id && <button className="rt-btn rt-ghost" type="button" onClick={() => void cancelResearchRun(selectedResult.run_id!)}>Cancel run</button>}
        </section>}
        {brief ? <ResearchBriefView brief={brief} instrument={selected} horizon={horizon} onHorizon={setHorizon} sentimentHistory={selectedSentimentHistory} />
          : <section className="rt-card rt-empty-state"><h3>No research report yet</h3><p>Run research to calculate component scores, horizon assessments and evidence for {selected.symbol}.</p></section>}

        <details className="rt-card rt-collapsible">
          <summary>Custom source URLs · {sourceLinks.length}</summary>
        <section className="research-source-manager">
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

        </details>

        <details className="rt-card rt-collapsible my-notes-section">
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
      {!selected && <section className="rt-card rt-empty-state"><h3>Select a stock to begin</h3><p>Search for an instrument, choose a horizon and run research to see scored, evidence-linked assessments.</p></section>}
    </div>
  );
}
