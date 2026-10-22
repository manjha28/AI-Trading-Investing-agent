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
    data_quality: { status: string; items: QualityItem[] };
    disclaimer: string;
  };
};
type FactDraft = { statement: string; as_of: string; source_ids: string[] };
type BatchRunResult = {
  instrument: Instrument;
  brief: Brief | null;
  error: string | null;
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
        if (!instrument.is_synthetic) {
          const now = new Date();
          const start = new Date(now);
          start.setUTCFullYear(start.getUTCFullYear() - 2);
          const syncQuery = new URLSearchParams({
            start: start.toISOString(),
            end: now.toISOString(),
            interval: '1d',
            limit: '500',
          });
          const [historyResult, fundamentalsResult] = await Promise.allSettled([
            apiRequest<{ bars: unknown[] }>(
              `/market-data/instruments/${instrument.id}/history/sync?${syncQuery}`,
              { method: 'POST' },
            ),
            apiRequest<{ status: 'available' | 'unavailable'; message: string }>(
              `/market-data/instruments/${instrument.id}/fundamentals/sync`,
              { method: 'POST' },
            ),
          ]);
          if (historyResult.status === 'rejected') {
            syncWarnings.push(`${instrument.symbol}: historical data sync failed.`);
          } else if (historyResult.value.bars.length === 0) {
            syncWarnings.push(`${instrument.symbol}: no real daily bars were returned.`);
          }
          if (fundamentalsResult.status === 'rejected') {
            syncWarnings.push(`${instrument.symbol}: fundamentals sync failed.`);
          } else if (fundamentalsResult.value.status === 'unavailable') {
            syncWarnings.push(`${instrument.symbol}: ${fundamentalsResult.value.message}`);
          }
        }
        const result = await apiRequest<Brief>(`/research/instruments/${instrument.id}/run`, {
          method: 'POST',
          body: '{}',
        });
        successCount += 1;
        setBatchResults((current) => [...current, { instrument, brief: result, error: null }]);
        if (selected?.id === instrument.id) {
          setBrief(result);
          try {
            await refreshNotesAndSources(instrument.id);
          } catch (refreshError) {
            setError(refreshError instanceof Error ? refreshError.message : 'Research sources saved, but the page could not refresh its source list.');
          }
        }
      } catch (requestError) {
        const message = requestError instanceof Error ? requestError.message : 'Research run failed.';
        setBatchResults((current) => [...current, { instrument, brief: null, error: message }]);
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

  return (
    <div className="backfill-page research-pipeline-page">
      <p className="decision-disclaimer">Decision support, not investment advice. The template brief uses stored data and cited sources; it does not use AI to calculate or invent figures.</p>
      {error && <p className="form-error" role="alert">{error}</p>}
      {notice && <p className="alerts-notice" role="status">{notice}</p>}

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
        {batchResults.map(({ instrument, brief: result, error: resultError }) => <details className="data-section research-batch-result" key={instrument.id} open={batchResults.length === 1}>
          <summary><strong>{instrument.symbol} · {instrument.display_name}</strong><span className={resultError ? 'research-run-status research-status-failed' : `research-run-status research-status-${result?.status ?? 'failed'}`}>{resultError ? 'request failed' : result?.status}</span></summary>
          {resultError ? <p className="form-error" role="alert">{resultError}</p> : result && <ResearchBriefView brief={result} />}
        </details>)}
      </section>}

      {selected && <>
        <section className="data-section research-selected-panel">
          <div className="section-heading">
            <div><p className="eyebrow">SELECTED INSTRUMENT</p><h2>{selected.symbol} · {selected.display_name}</h2></div>
            <button className="primary-button" type="button" onClick={() => void runResearch()} disabled={busy}>{busy ? 'Researching…' : 'Run research'}</button>
          </div>
          <p className="research-subtitle">{selected.venue} · Template renderer · Reads stored fundamentals and OHLCV; never fetches or stores full page bodies.</p>
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

        {brief && batchResults.length === 0 && <ResearchBriefView brief={brief} />}

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

export function ResearchBriefView({ brief }: { brief: Brief }) {
  const { fundamentals, technicals, financials_trend: trend, news, data_quality: quality } = brief.sections;
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
    : Number(value).toLocaleString(undefined, { maximumFractionDigits: 4 });
  return <section className="data-section research-brief">
    <div className="section-heading"><div><p className="eyebrow">RESEARCH BRIEF · {brief.renderer.toUpperCase()}</p><h2>Evidence and data</h2></div><span className={`research-run-status research-status-${brief.status}`}>{brief.status}</span></div>
    <p className="research-subtitle">Sources succeeded: {brief.sources_succeeded}/{brief.sources_attempted} · generated {new Date(brief.created_at).toLocaleString()}</p>
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

    <section className="research-brief-section">
      <h3>Fundamentals</h3>
      <p className="research-status-line">{fundamentals.status === 'unavailable' ? 'Unavailable from stored providers.' : 'Stored M5.3 fundamentals; values are not estimated.'}</p>
      {fundamentals.snapshot.status === 'available' && fundamentals.snapshot.values && <>
        <div className="research-snapshot-grid">{Object.entries(fundamentals.snapshot.values).map(([name, item]) => <article key={name}>
          <small>{name.replaceAll('_', ' ')}</small><strong>{item.value === null ? 'Unavailable' : item.value}</strong>
        </article>)}</div>
        <p className="research-subtitle">Source: {fundamentals.snapshot.source} · fetched {new Date(fundamentals.snapshot.fetched_at ?? '').toLocaleString()} {fundamentals.snapshot.stale && <span className="stale-marker">STALE</span>}</p>
      </>}
      {fundamentals.periods.length > 0 && <div className="research-period-list">
        {fundamentals.periods.map((period) => <article className="research-period-card" key={`${period.period_type}-${period.period_end}`}>
          <header><strong>{period.period_type} · {period.period_end}</strong><span>{period.currency} · {period.source}{period.stale && <span className="stale-marker">STALE</span>}</span></header>
          <div className="research-value-grid">{Object.entries(period.values).map(([name, item]) => <span key={name}><small>{name.replaceAll('_', ' ')}</small><strong>{displayValue(item.value)}</strong></span>)}</div>
          <small>Fetched {new Date(period.fetched_at).toLocaleString()}</small>
        </article>)}
      </div>}
    </section>

    <section className="research-brief-section">
      <h3>Technicals · stored daily OHLCV</h3>
      {technicals.message && <p className="research-status-line">{technicals.message}</p>}
      <div className="research-value-grid">{Object.entries(technicals.indicators).map(([name, item]) => <span key={name}>
        <small>{name.replaceAll('_', ' ')}</small>
        <strong>{item.value === null ? 'Insufficient history' : displayValue(item.value)}</strong>
        <small>{item.bars_used}/{item.bars_required} bars{item.is_synthetic ? ' · synthetic' : ''}</small>
      </span>)}</div>
      {technicals.source && <small>Source {technicals.source} · latest bar {technicals.latest_bar ? new Date(technicals.latest_bar).toLocaleString() : 'unavailable'}</small>}
    </section>

    <section className="research-brief-section">
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

    <section className="research-brief-section">
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
