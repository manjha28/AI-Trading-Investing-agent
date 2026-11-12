import { useCallback, useEffect, useMemo, useState } from 'react';
import { apiRequest } from '../api';
import { Empty, Field, Notice, Pill, Stat } from './shared';
import './derivatives-data.css';

type Coverage = {
  scope: string; data_type: string; category: string; exchange: string; segment: string; instrument_type: string;
  underlying: string | null; timeframe: string; provenance: string; contract_state: string; instruments: number; contracts: number;
  rows: number; first: string | null; last: string | null; sessions: number; missing: { present?: number; expected_total?: number } | null;
  last_success_at: string | null; last_status: string | null; last_error: string | null; provider: string; blocker: string | null; status: string;
};
type Contract = { id: string; exchange: string; segment: string; instrument_type: string; security_id: string; underlying: string; trading_symbol: string;
  expiry: string; strike?: string; option_type?: 'CE' | 'PE'; status: string; lot_size: string | null; tick_size: string | null;
  coverage: { candles_1m: number; first: string | null; last: string | null; sessions: number; missing_intervals: { estimated_missing_minutes: number; note: string } | null;
    invalid_candles_skipped: number; warnings: string[]; last_success_at: string | null; provider: string; status: string;
    latest_live: null | { captured_at: string; bid: string | null; ask: string | null; iv: string | null; oi: number | null } } };
type Underlying = { id: string; exchange: string; segment: string; category: string; symbol: string; has_futures: boolean; has_options: boolean; status: string };
type Job = { id: string; kind: string; status: string; priority: number; progress_pct: number; tasks_total: number; tasks_done: number; tasks_failed: number;
  attempts: number; rows_written: number; error: string | null; cancel_requested: boolean; created_at: string; started_at: string | null; finished_at: string | null; params: Record<string, any> };
type Snapshot = { id: string; exchange: string; underlying: string; expiry: string; captured_at: string; strikes: number; quotes_stored: number; quotes_with_bid_ask: number };
type Universe = { underlyings: Array<{ exchange: string; segment: string; category: string; underlyings: number }>; contracts: Array<{ kind: string; exchange: string; segment: string; instrument_type: string; status: string; contracts: number; underlyings: number }>; master: null | { fetched_at: string; rows_total: number; derivative_rows: number; new: number; changed: number; expired: number; removed: number } };
type Capabilities = { capabilities: Record<string, { status: string; note?: string }>; limits: Record<string, number | string> };
const PAGE_SIZE = 50;
const fmt = (v: number | null | undefined) => v == null ? '—' : new Intl.NumberFormat().format(v);
const stamp = (v: string | null | undefined) => v ? new Date(v).toLocaleString() : '—';
const paramsOf = (values: Record<string, string | number | undefined>) => {
  const p = new URLSearchParams(); Object.entries(values).forEach(([k, v]) => { if (v !== '' && v != null) p.set(k, String(v)); }); return p.toString();
};
const DATA_TYPES = [
  ['futures_candles_1m', 'Futures 1m'], ['futures_candles_1d', 'Futures daily'], ['option_candles_1m_active', 'Active options 1m'],
  ['option_candles_1m_rolling', 'Expired rolling options'], ['option_chain_snapshots_live', 'Live chain snapshots'],
  ['option_bid_ask_historical', 'Bid/ask capture'], ['option_iv_oi_rolling', 'Rolling IV / OI'], ['futures_expired_1m', 'Expired futures 1m'],
] as const;

export function DerivativesData() {
  const [coverage, setCoverage] = useState<Coverage[]>([]);
  const [overviewCoverage, setOverviewCoverage] = useState<Coverage[]>([]);
  const [universe, setUniverse] = useState<Universe | null>(null);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [capabilities, setCapabilities] = useState<Capabilities | null>(null);
  const [contracts, setContracts] = useState<Contract[]>([]);
  const [contractTotal, setContractTotal] = useState(0);
  const [underlyings, setUnderlyings] = useState<Underlying[]>([]);
  const [underlyingTotal, setUnderlyingTotal] = useState(0);
  const [snapshots, setSnapshots] = useState<Snapshot[]>([]);
  const [filters, setFilters] = useState({ exchange: '', segment: '', category: '', kind: 'futures', underlying: '', expiry: '', optionType: '', status: '', provenance: '', dataType: '', timeframe: '' });
  const [search, setSearch] = useState('');
  const [sortBy, setSortBy] = useState('category');
  const [page, setPage] = useState(0);
  const [uPage, setUPage] = useState(0);
  const [selectedUnderlying, setSelectedUnderlying] = useState('');
  const [selectedContractId, setSelectedContractId] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState('');
  const [dryRun, setDryRun] = useState<Record<string, any> | null>(null);
  const [showStart, setShowStart] = useState(false);
  const [jobForm, setJobForm] = useState({ kind: 'futures', exchange: 'NSE', segment: 'NSE_FNO', underlying: '', days: 10, limit: 100, half_width: 2, expiries: 1, priority: '' });

  const loadOverview = useCallback(async () => {
    const [u, c, j, cap, s] = await Promise.all([
      apiRequest<Universe>('/derivatives/universe'),
      apiRequest<{ rows: Coverage[] }>('/derivatives/coverage'),
      apiRequest<Job[]>('/derivatives/jobs?limit=200'),
      apiRequest<Capabilities>('/derivatives/capabilities'),
      apiRequest<Snapshot[]>('/derivatives/chain-snapshots?limit=100'),
    ]);
    setUniverse(u); setOverviewCoverage(c.rows); setJobs(j); setCapabilities(cap); setSnapshots(s);
  }, []);

  const loadContracts = useCallback(async () => {
    if (!selectedUnderlying) { setContracts([]); setContractTotal(0); return; }
    const q = paramsOf({ kind: filters.kind, exchange: filters.exchange, segment: filters.segment, underlying: selectedUnderlying,
      expiry: filters.expiry, option_type: filters.kind === 'options' ? filters.optionType : '', status: filters.status, limit: PAGE_SIZE, offset: page * PAGE_SIZE });
    const result = await apiRequest<{ total: number; items: Contract[] }>(`/derivatives/contracts?${q}`);
    setContracts(result.items); setContractTotal(result.total);
  }, [filters, page, selectedUnderlying]);

  const loadUnderlyings = useCallback(async () => {
    const q = paramsOf({ exchange: filters.exchange, category: filters.category, q: search.trim(), limit: 25, offset: uPage * 25 });
    const result = await apiRequest<{ total: number; items: Underlying[] }>(`/derivatives/underlyings?${q}`);
    setUnderlyings(result.items); setUnderlyingTotal(result.total);
  }, [filters.exchange, filters.category, search, uPage]);

  const loadCoverage = useCallback(async () => {
    const q = paramsOf({ exchange: filters.exchange, segment: filters.segment, category: filters.category, data_type: filters.dataType,
      provenance: filters.provenance, contract_state: filters.status, timeframe: filters.timeframe });
    const result = await apiRequest<{ rows: Coverage[] }>(`/derivatives/coverage?${q}`);
    setCoverage(result.rows);
  }, [filters, selectedUnderlying]);

  const reload = useCallback(async (initial = false) => {
    if (initial) setLoading(true);
    try { await Promise.all([loadOverview(), loadContracts(), loadUnderlyings(), loadCoverage()]); setError(''); }
    catch (e) { setError(e instanceof Error ? e.message : 'Unable to load derivatives data.'); }
    finally { if (initial) setLoading(false); }
  }, [loadContracts, loadCoverage, loadOverview, loadUnderlyings]);

  useEffect(() => { void reload(true); }, [reload]);
  useEffect(() => { const id = window.setInterval(() => { void reload(false); }, 15000); return () => window.clearInterval(id); }, [reload]);
  useEffect(() => { setPage(0); }, [filters.kind, filters.exchange, filters.segment, filters.status, filters.optionType, filters.expiry, selectedUnderlying]);

  const setFilter = (key: keyof typeof filters, value: string) => setFilters((f) => ({ ...f, [key]: value }));
  const latestSnapshot = snapshots[0];
  const snapshotAgeMinutes = latestSnapshot ? Math.max(0, Math.floor((Date.now() - new Date(latestSnapshot.captured_at).getTime()) / 60_000)) : null;
  const metrics = useMemo(() => {
    const byType = (type: string) => overviewCoverage.filter((r) => r.data_type === type).reduce((n, r) => n + r.rows, 0);
    return {
      contracts: universe?.contracts.reduce((n, r) => n + r.contracts, 0) ?? 0,
      underlyings: universe?.underlyings.reduce((n, r) => n + r.underlyings, 0) ?? 0,
      fut1m: byType('futures_candles_1m'), activeOptions: byType('option_candles_1m_active'), rolling: byType('option_candles_1m_rolling'),
      chains: byType('option_chain_snapshots_live'), activeJobs: jobs.filter((j) => ['running', 'queued'].includes(j.status)).length,
      failedJobs: jobs.filter((j) => ['failed', 'partial', 'dead_letter'].includes(j.status)).length,
    };
  }, [jobs, overviewCoverage, universe]);

  const visibleCoverage = useMemo(() => {
    const needle = search.trim().toLowerCase();
    return [...coverage].filter((r) => !needle || [r.scope, r.exchange, r.segment, r.instrument_type, r.underlying, r.data_type].some((v) => v?.toLowerCase().includes(needle)))
      .sort((a, b) => sortBy === 'rows' ? b.rows - a.rows : sortBy === 'contracts' ? b.contracts - a.contracts : sortBy === 'latest' ? (b.last ?? '').localeCompare(a.last ?? '') : a.data_type.localeCompare(b.data_type) || a.exchange.localeCompare(b.exchange) || a.segment.localeCompare(b.segment) || a.instrument_type.localeCompare(b.instrument_type));
  }, [coverage, search, sortBy]);
  const matchingJob = jobs.find((j) => {
    const scope = j.params?.scope ?? {}; const args = j.params?.request ?? j.params?.args ?? {};
    const sameScope = jobForm.underlying ? scope.underlying === jobForm.underlying : scope.segment === jobForm.segment;
    return j.kind === `${jobForm.kind}_backfill` && ['queued', 'running', 'succeeded'].includes(j.status) && sameScope
      && Number(scope.limit ?? 0) === Number(jobForm.limit) && (args.kind ?? jobForm.kind) === jobForm.kind && Number(args.days) === Number(jobForm.days)
      && Number(args.expiries ?? 1) === Number(jobForm.expiries) && Number(args.half_width ?? 5) === Number(jobForm.half_width);
  });

  async function openPlanner() {
    if (matchingJob) { setNotice(`A matching ${matchingJob.status} job already exists (${matchingJob.id}); no duplicate request was created.`); return; }
    setBusy('plan'); setError(''); setNotice('');
    try {
      const body = { ...jobForm, kind: jobForm.kind, days: Number(jobForm.days), limit: Number(jobForm.limit), half_width: Number(jobForm.half_width), expiries: Number(jobForm.expiries), priority: jobForm.priority.split(/[ ,]+/).filter(Boolean), dry_run: true };
      const result = await apiRequest<Record<string, any>>('/derivatives/jobs', { method: 'POST', body: JSON.stringify(body) }); setDryRun({ ...result, request: body }); setShowStart(false);
    } catch (e) { setError(e instanceof Error ? e.message : 'Could not estimate this backfill.'); }
    finally { setBusy(''); }
  }
  async function confirmStart() {
    if (!dryRun?.request || dryRun.requests > 1500) return;
    setBusy('start');
    try {
      const result = await apiRequest<Record<string, any>>('/derivatives/jobs', { method: 'POST', body: JSON.stringify({ ...dryRun.request, dry_run: false }) });
      setNotice(`Backfill started: ${result.job.id} · ${result.requests} requests estimated. Data counts increase only after records are persisted.`); setDryRun(null); await reload(false);
    } catch (e) { setError(e instanceof Error ? e.message : 'Could not start the backfill.'); }
    finally { setBusy(''); }
  }
  async function jobAction(job: Job, action: 'cancel' | 'resume') {
    setBusy(job.id);
    try { await apiRequest(`/derivatives/jobs/${job.id}/${action}`, { method: 'POST', body: '{}' }); setNotice(`${action === 'cancel' ? 'Cancellation requested for' : 'Resumed'} job ${job.id}.`); await reload(false); }
    catch (e) { setError(e instanceof Error ? e.message : `Could not ${action} this job.`); }
    finally { setBusy(''); }
  }
  async function refreshCoverage() {
    setBusy('coverage'); setError('');
    try { await apiRequest('/derivatives/coverage/refresh', { method: 'POST', body: '{}' }); await reload(false); setNotice('Coverage recomputed from persisted market-data rows.'); }
    catch (e) { setError(e instanceof Error ? e.message : 'Could not refresh coverage.'); }
    finally { setBusy(''); }
  }

  const setJob = (key: string, value: string | number) => setJobForm((f) => ({ ...f, [key]: value }));
  const chainRows = overviewCoverage.filter((r) => r.data_type === 'option_chain_snapshots_live').reduce((n, r) => n + r.rows, 0);
  const chainQuotes = snapshots.reduce((n, s) => n + s.quotes_stored, 0);
  const selectedContract = contracts.find((c) => c.id === selectedContractId) ?? null;
  const loadedExpiries = [...new Set(contracts.map((c) => c.expiry))].sort();
  const loadedOptionSides = [...new Set(contracts.flatMap((c) => c.option_type ? [c.option_type] : []))];
  const loadedStrikes = [...new Set(contracts.flatMap((c) => c.strike ? [Number(c.strike)] : []))].sort((a, b) => a - b);

  return <section className="sl-root dd-root" aria-label="Derivatives data">
    <header className="sl-head"><div><p className="eyebrow">DHAN · HISTORICAL AND LIVE DATA</p><h1>Derivatives Data</h1></div><div className="dd-head-status"><Pill tone={universe?.master ? 'ok' : 'warn'}>{universe?.master ? 'Master loaded' : 'Master unavailable'}</Pill><span className="sl-muted">Coverage counts are persisted records.</span></div></header>
    {error && <Notice tone="bad">{error}</Notice>}{notice && <Notice tone="info">{notice}</Notice>}
    {loading && !universe ? <div className="dd-loading">Loading derivatives coverage and jobs…</div> : <>
      <div className="sl-stats dd-stats">
        <Stat label="Derivative contracts" value={fmt(metrics.contracts)} /><Stat label="Underlyings" value={fmt(metrics.underlyings)} />
        <Stat label="Futures 1m candles" value={fmt(metrics.fut1m)} /><Stat label="Active options 1m" value={fmt(metrics.activeOptions)} />
        <Stat label="Rolling expired options" value={fmt(metrics.rolling)} /><Stat label="Live chain snapshots" value={fmt(metrics.chains)} />
        <Stat label="Active jobs" value={fmt(metrics.activeJobs)} /><Stat label="Failed jobs" value={fmt(metrics.failedJobs)} tone={metrics.failedJobs ? 'neg' : undefined} />
      </div>
      <Notice tone={metrics.contracts && (metrics.fut1m || metrics.activeOptions || metrics.rolling || metrics.chains) ? 'ok' : 'warn'}>
        {metrics.contracts ? `${fmt(metrics.contracts)} contracts discovered; ${fmt(metrics.fut1m + metrics.activeOptions + metrics.rolling + metrics.chains)} persisted records are reflected below. Queued requests are excluded.` : 'No contract master data is available yet.'}
        {universe?.master && <> Master refresh {stamp(universe.master.fetched_at)} · changes: +{universe.master.new}, revised {universe.master.changed}, expired {universe.master.expired}, removed {universe.master.removed}.</>}
      </Notice>

      <section className="sl-card"><div className="sl-card-head"><h2>Coverage filters</h2><button type="button" disabled={busy === 'coverage'} onClick={() => void refreshCoverage()}>{busy === 'coverage' ? 'Refreshing…' : 'Recompute persisted coverage'}</button></div>
        <div className="dd-filter-grid">
          <Field label="Exchange"><select value={filters.exchange} onChange={(e) => { setFilter('exchange', e.target.value); setUPage(0); setSelectedUnderlying(''); }}><option value="">All exchanges</option>{['NSE','BSE','MCX'].map(x => <option key={x}>{x}</option>)}</select></Field>
          <Field label="Segment"><select value={filters.segment} onChange={(e) => setFilter('segment', e.target.value)}><option value="">All segments</option>{['NSE_FNO','BSE_FNO','MCX_COMM','NSE_COMM','NSE_CURRENCY','BSE_CURRENCY'].map(x => <option key={x}>{x}</option>)}</select></Field>
          <Field label="Category"><select value={filters.category} onChange={(e) => { setFilter('category', e.target.value); setUPage(0); setSelectedUnderlying(''); }}><option value="">All categories</option>{['index','stock','commodity','currency'].map(x => <option key={x}>{x}</option>)}</select></Field>
          <Field label="Data type"><select value={filters.dataType} onChange={(e) => setFilter('dataType', e.target.value)}><option value="">All data types</option>{DATA_TYPES.map(([v,l]) => <option key={v} value={v}>{l}</option>)}</select></Field>
          <Field label="Provenance"><select value={filters.provenance} onChange={(e) => setFilter('provenance', e.target.value)}><option value="">Historical and live</option><option value="historical">Historical</option><option value="live">Live</option></select></Field>
          <Field label="Timeframe"><select value={filters.timeframe} onChange={(e) => setFilter('timeframe', e.target.value)}><option value="">All timeframes</option>{['1m','1d','snapshot'].map(x => <option key={x}>{x}</option>)}</select></Field>
        </div>
      </section>

      <section className="sl-card"><div className="sl-card-head"><div><h2>Persisted coverage by data category</h2><p className="dd-sub">Server-filtered segment and instrument summaries; contract counts and candle rows are separate measures.</p></div><div className="dd-table-tools"><input aria-label="Search coverage" placeholder="Search scope, symbol, segment…" value={search} onChange={(e) => setSearch(e.target.value)} /><label>Sort <select value={sortBy} onChange={(e) => setSortBy(e.target.value)}><option value="category">Category</option><option value="rows">Most rows</option><option value="contracts">Most contracts</option><option value="latest">Latest data</option></select></label></div></div>
        {visibleCoverage.length ? <div className="dd-table-wrap"><table className="sl-table sl-compact"><thead><tr><th>Data category</th><th>Underlying</th><th>Exchange / segment</th><th>Instrument</th><th className="sl-r">Contracts</th><th className="sl-r">Rows</th><th>First available</th><th>Last available</th><th>Status / limitation</th><th>Last successful sync</th><th>Provider</th></tr></thead><tbody>
          {visibleCoverage.map(r => <tr key={r.scope}><td>{r.data_type.replaceAll('_',' ')}</td><td>{r.underlying ?? `All ${fmt(r.instruments)} underlyings`}</td><td>{r.exchange} · {r.segment}</td><td>{r.instrument_type}</td><td className="sl-r">{fmt(r.contracts)}</td><td className="sl-r">{fmt(r.rows)}</td><td>{stamp(r.first)}</td><td>{stamp(r.last)}</td><td><Pill tone={r.blocker ? 'warn' : r.rows ? 'ok' : 'neutral'}>{r.blocker ?? r.status}</Pill>{r.last_error && <div className="dd-warning">{r.last_error}</div>}</td><td>{stamp(r.last_success_at)}</td><td>{r.provider}</td></tr>)}
        </tbody></table></div> : <Empty title="No matching coverage" />}
      </section>

      <section className="sl-card"><div className="sl-card-head"><div><h2>Contract explorer</h2><p className="dd-sub">Search and page the instrument master first. Contract rows are fetched from the server for the selected underlying.</p></div></div>
        <div className="dd-explorer-tools"><Field label="Find underlying"><input value={search} placeholder="Symbol or name" onChange={(e) => { setSearch(e.target.value); setUPage(0); }} /></Field><div className="dd-page-control"><button type="button" disabled={!uPage} onClick={() => setUPage(p => p - 1)}>Previous</button><span>{fmt(underlyingTotal)} underlyings · page {uPage + 1}</span><button type="button" disabled={(uPage + 1) * 25 >= underlyingTotal} onClick={() => setUPage(p => p + 1)}>Next</button></div></div>
        {underlyings.length > 0 && <div className="dd-underlying-list">{underlyings.map(u => <button key={u.id} type="button" className={selectedUnderlying === u.symbol ? 'active' : ''} onClick={() => { setSelectedUnderlying(u.symbol); setFilter('exchange', filters.exchange || u.exchange); setPage(0); }}>{u.symbol}<small>{u.exchange} · {u.segment} · {u.category}</small></button>)}</div>}
        <div className="dd-filter-grid dd-contract-filters">
          <Field label="Derivative type"><select value={filters.kind} onChange={(e) => { setFilter('kind', e.target.value); setPage(0); }}><option value="futures">Futures</option><option value="options">Options</option></select></Field>
          <Field label="Expiry"><input type="date" value={filters.expiry} onChange={(e) => setFilter('expiry', e.target.value)} /></Field>
          {filters.kind === 'options' && <Field label="Option type"><select value={filters.optionType} onChange={(e) => setFilter('optionType', e.target.value)}><option value="">CE and PE</option><option>CE</option><option>PE</option></select></Field>}
          <Field label="Contract status"><select value={filters.status} onChange={(e) => setFilter('status', e.target.value)}><option value="">Active and expired</option><option value="active">Active</option><option value="expired">Expired</option><option value="removed">Removed</option></select></Field>
        </div>
        {!selectedUnderlying ? <Empty title="Select an underlying" /> : <>
          <div className="dd-table-wrap"><table className="sl-table sl-compact"><thead><tr><th>Underlying / contract</th><th>Exchange</th><th>Segment</th><th>Instrument type</th><th>Expiry</th><th>CE/PE</th><th className="sl-r">Contracts</th><th className="sl-r">1m candles</th><th>First available</th><th>Last available</th><th>Coverage</th><th>Last successful sync</th><th>Provider / warnings</th></tr></thead><tbody>
            {contracts.map(c => <tr key={c.id} className={selectedContractId === c.id ? 'sl-selected' : 'sl-click'} onClick={() => setSelectedContractId(c.id)} onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') setSelectedContractId(c.id); }} tabIndex={0} aria-selected={selectedContractId === c.id}><td><strong>{c.underlying}</strong><div className="sl-muted">{c.trading_symbol}</div>{c.strike && <span>Strike {c.strike}</span>}</td><td>{c.exchange}</td><td>{c.segment}</td><td>{c.instrument_type}</td><td>{c.expiry}</td><td>{c.option_type ?? '—'}</td><td className="sl-r">1</td><td className="sl-r">{fmt(c.coverage.candles_1m)}</td><td>{stamp(c.coverage.first)}</td><td>{stamp(c.coverage.last)}</td><td><Pill tone={c.coverage.candles_1m ? 'ok' : 'neutral'}>{c.coverage.status}</Pill></td><td>{stamp(c.coverage.last_success_at)}</td><td>{c.coverage.provider}{c.coverage.invalid_candles_skipped > 0 && <div className="dd-warning">{c.coverage.invalid_candles_skipped} invalid/skipped</div>}{c.coverage.warnings[0] && <div className="dd-warning">{c.coverage.warnings[0]}</div>}</td></tr>)}
            {!contracts.length && <tr><td colSpan={13}><Empty title="No matching contracts" /></td></tr>}
          </tbody></table></div>
          <div className="dd-page-control"><span>{fmt(contractTotal)} matching contracts · page {page + 1} · {PAGE_SIZE} per page</span><button type="button" disabled={!page} onClick={() => setPage(p => p - 1)}>Previous</button><button type="button" disabled={(page + 1) * PAGE_SIZE >= contractTotal} onClick={() => setPage(p => p + 1)}>Next</button></div>
          <div className="dd-detail-grid"><div><strong>Available contracts and expiry/strike coverage</strong><span>{fmt(contractTotal)} contracts for {selectedUnderlying}; this server page has {loadedExpiries.length} expiries {loadedExpiries.slice(0,4).join(', ') || '—'}, strikes {loadedStrikes.length ? `${loadedStrikes[0]}–${loadedStrikes.at(-1)} (${loadedStrikes.length} listed strikes on page)` : '—'}, and {loadedOptionSides.join('/ ') || 'no option-side'} coverage.</span></div><div><strong>Selected contract history</strong><span>{selectedContract ? `${selectedContract.trading_symbol}: ${fmt(selectedContract.coverage.candles_1m)} 1-minute candles, ${stamp(selectedContract.coverage.first)} to ${stamp(selectedContract.coverage.last)}.` : 'Select a contract row to inspect its historical range.'} {selectedContract?.coverage.missing_intervals ? `Estimated missing intervals: ${fmt(selectedContract.coverage.missing_intervals.estimated_missing_minutes)} minutes. ${selectedContract.coverage.missing_intervals.note}` : ''}</span></div><div><strong>Data quality and latest live option quote</strong><span>{selectedContract ? `${fmt(selectedContract.coverage.invalid_candles_skipped)} invalid/skipped candles. ${selectedContract.coverage.latest_live ? `Latest quote ${stamp(selectedContract.coverage.latest_live.captured_at)} · bid ${selectedContract.coverage.latest_live.bid ?? '—'} / ask ${selectedContract.coverage.latest_live.ask ?? '—'} · IV ${selectedContract.coverage.latest_live.iv ?? '—'} · OI ${fmt(selectedContract.coverage.latest_live.oi)}.` : 'No persisted live quote for this selected contract.'}` : 'Select a contract row to inspect data quality and latest live data.'}</span></div></div>
        </>}
      </section>

      <section className="sl-card"><div className="sl-card-head"><div><h2>Data readiness</h2><p className="dd-sub">Provider probe findings, kept separate from ingestion status.</p></div></div><div className="dd-readiness">
        {(['index','stock'] as const).map((category) => <div key={category}><span>Underlying {category} candles</span><Pill tone="info">Market Data scope</Pill><small>Tracked by the existing owner-scoped Market Data coverage; separate from derivative candle totals.</small></div>)}
        {[
          ['Futures historical', 'futures_intraday_1m', 'Expired futures minute history was unobtainable from tested endpoints; daily history is provider_rolled.'],
          ['Active options historical', 'options_active_intraday_1m', 'Active option 1-minute OHLCV and OI were verified.'],
          ['Expired rolling options', 'options_rolling_1m', 'Measured request span is two calendar days; stock weekly, NSE commodity and MCX index options are unsupported in tested requests.'],
          ['Live option chains', 'option_chain_live', 'Tested for NSE index, NSE stock, BSE index and MCX. Capture time is local because provider timestamps are absent.'],
          ['Historical bid/ask', 'historical_bid_ask', 'Unavailable in tested historical responses; only actual captured live snapshots contain bid/ask.'],
          ['Contract master', 'master', 'Daily Dhan master ingestion tracks added, changed, expired and removed rows.'],
        ].map(([label, key, note]) => { const cap = capabilities?.capabilities[key]; return <div key={key}><span>{label}</span><Pill tone={cap?.status === 'ok' ? 'ok' : cap?.status === 'partial' ? 'warn' : 'bad'}>{cap?.status ?? 'unknown'}</Pill><small>{note}{cap?.note ? ` ${cap.note}` : ''}</small></div>; })}
      </div><p className="dd-sub">Live-chain summary: {fmt(chainRows)} persisted snapshots · {fmt(chainQuotes)} quotes in latest 100 snapshots · last capture {stamp(latestSnapshot?.captured_at)} · freshness {snapshotAgeMinutes == null ? 'unavailable' : `${snapshotAgeMinutes} min`} <Pill tone={snapshotAgeMinutes != null && snapshotAgeMinutes < 5 ? 'ok' : 'warn'}>{snapshotAgeMinutes != null && snapshotAgeMinutes < 5 ? 'under 5 min' : 'older than 5 min'}</Pill>.</p></section>

      <section className="sl-card"><div className="sl-card-head"><div><h2>Ingestion jobs</h2><p className="dd-sub">Queued requests are progress only; rows written count persisted data.</p></div><button type="button" className="sl-primary" onClick={() => { setShowStart(true); setError(''); }}>Plan backfill</button></div>
        {jobs.length ? <div className="dd-table-wrap"><table className="sl-table sl-compact"><thead><tr><th>Job ID / type</th><th>Exchange / underlying</th><th>Contract / expiry</th><th>Date range</th><th>Status / progress</th><th>Attempts</th><th>Last error</th><th>Created / started / finished</th><th>Control</th></tr></thead><tbody>{jobs.map(j => {
          const sc = j.params?.scope ?? {}; const arg = j.params?.request ?? j.params?.args ?? {}; const cancelable = j.status === 'running' || j.status === 'queued'; const resumable = ['failed','partial','interrupted'].includes(j.status);
          return <tr key={j.id}><td><code>{j.id}</code><div>{j.kind}</div></td><td>{sc.exchange ?? sc.segment ?? arg.exchange ?? '—'} / {sc.underlying ?? arg.underlying ?? '—'}</td><td>{sc.instrument_type ?? '—'} / {arg.expiries ? `${arg.expiries} expiry request(s)` : '—'}</td><td>{arg.days ? `last ${arg.days} days` : '—'}</td><td><Pill tone={j.status === 'succeeded' ? 'ok' : ['running','queued'].includes(j.status) ? 'warn' : j.status === 'failed' || j.status === 'partial' ? 'bad' : 'neutral'}>{j.status}</Pill><div className="dd-progress"><span style={{ width: `${Math.min(100, j.progress_pct)}%` }} /></div>{j.progress_pct}% · {j.tasks_done + j.tasks_failed}/{j.tasks_total} tasks · {fmt(j.rows_written)} rows</td><td>{fmt(j.attempts)}</td><td className="dd-error">{j.error ?? '—'}</td><td>{stamp(j.created_at)}<div>{stamp(j.started_at)} / {stamp(j.finished_at)}</div></td><td><div className="dd-actions">{cancelable && <button type="button" disabled={busy === j.id} onClick={() => void jobAction(j,'cancel')}>Cancel</button>}{resumable && <button type="button" disabled={busy === j.id} onClick={() => void jobAction(j,'resume')}>Resume / retry</button>}<button type="button" onClick={async () => { try { const d = await apiRequest<any>(`/derivatives/jobs/${j.id}`); setNotice(`${j.id}: ${JSON.stringify(d.task_counts ?? {})}; ${d.errors?.slice(0,3).map((x:any) => x.error).join(' · ') || 'No task errors.'}`); } catch (e) { setError(String(e)); } }}>Details</button></div></td></tr>;
        })}</tbody></table></div> : <Empty title="No ingestion jobs yet" />}
      </section>
    </>}

    {showStart && <div className="sl-modal-backdrop" onKeyDown={(e) => { if (e.key === 'Escape') setShowStart(false); }}><div className="sl-modal dd-backfill-modal" role="dialog" aria-modal="true" aria-label="Plan derivatives backfill"><h3>Plan a scoped backfill</h3><p className="dd-sub">The first action is estimate-only. No provider requests happen until you review and confirm.</p>
      <div className="dd-filter-grid"><Field label="Data type"><select value={jobForm.kind} onChange={(e) => setJob('kind',e.target.value)}><option value="futures">Futures</option><option value="option_candles">Active option candles</option><option value="rolling">Expired rolling options</option><option value="chains">Live option chains</option></select></Field><Field label="Exchange"><select value={jobForm.exchange} onChange={(e) => setJob('exchange',e.target.value)}><option>NSE</option><option>BSE</option><option>MCX</option></select></Field><Field label="Segment"><select value={jobForm.segment} onChange={(e) => setJob('segment',e.target.value)}><option>NSE_FNO</option><option>BSE_FNO</option><option>MCX_COMM</option></select></Field><Field label="Underlying (optional)"><input maxLength={64} value={jobForm.underlying} onChange={(e) => setJob('underlying',e.target.value.toUpperCase())} /></Field><Field label="Lookback days"><input type="number" min={1} max={89} value={jobForm.days} onChange={(e) => setJob('days',Number(e.target.value))} /></Field><Field label="Contract / underlying limit"><input type="number" min={1} max={500} value={jobForm.limit} onChange={(e) => setJob('limit',Number(e.target.value))} /></Field><Field label="Expiry count"><input type="number" min={1} max={3} value={jobForm.expiries} onChange={(e) => setJob('expiries',Number(e.target.value))} /></Field><Field label="ATM strike width"><input type="number" min={0} max={10} value={jobForm.half_width} onChange={(e) => setJob('half_width',Number(e.target.value))} /></Field></div>
      <div className="sl-modal-actions"><button type="button" onClick={() => setShowStart(false)}>Close</button><button type="button" className="sl-primary" disabled={busy === 'plan' || matchingJob !== undefined} onClick={() => void openPlanner()}>{busy === 'plan' ? 'Estimating…' : matchingJob ? `Matching ${matchingJob.status} job exists` : 'Estimate requests'}</button></div>
    </div></div>}
    {dryRun && <div className="sl-modal-backdrop"><div className="sl-modal" role="dialog" aria-modal="true" aria-label="Confirm backfill request"><h3>Review backfill scope</h3><div className="dd-plan-review"><div><strong>{fmt(dryRun.requests)} requests</strong><span>{dryRun.by_kind && Object.entries(dryRun.by_kind).map(([k,v]) => `${k}: ${v}`).join(' · ')}</span></div><div><strong>Minimum time</strong><span>{fmt(dryRun.min_seconds)} sec at measured rate limits</span></div><div><strong>Scope</strong><span>{JSON.stringify(dryRun.scope)}</span></div></div>{dryRun.requests > 1500 && <Notice tone="warn">This plan exceeds the single-action safety limit of 1,500 requests. Narrow the scope and estimate again.</Notice>}<div className="sl-modal-actions"><button type="button" onClick={() => setDryRun(null)}>Cancel</button><button type="button" className="sl-primary" disabled={busy === 'start' || dryRun.requests > 1500} onClick={() => void confirmStart()}>{busy === 'start' ? 'Starting…' : 'Confirm and start'}</button></div></div></div>}
    <p className="sl-disclaimer">Decision support, not investment advice. Derivatives coverage reports market data only and does not place orders.</p>
  </section>;
}
