import { useCallback, useEffect, useState } from 'react';
import { Strategies } from '../pages/Strategies';
import { Backtesting } from './Backtesting';
import { Builder, type Draft } from './Builder';
import { Dashboard } from './Dashboard';
import { ImportPanel } from './ImportPanel';
import { IndicatorLibrary } from './IndicatorLibrary';
import { Library } from './Library';
import { JobsPanel, ScanHistoryPanel, ScannerSchedulePanel } from './JobsScanner';
import { LiveSignals } from './LiveSignals';
import { RejectionsPanel } from './RejectionsPanel';
import { DerivativesData } from './DerivativesData';
import { defaultParams, emptyDefinition, lab, messageOf, nextAlias } from './lab';
import { Notice } from './shared';
import type { Definition, Health, InstrumentRow, Meta, Readiness, StrategyRow } from './types';
import './signals.css';

const TABS = [
  ['dashboard', 'Strategy Dashboard'], ['builder', 'Strategy Builder'], ['library', 'Strategy Library'], ['backtesting', 'Backtesting'],
  ['live', 'Live Signals'], ['jobs', 'Jobs & scanner'], ['indicators', 'Indicator Library'], ['import', 'Import & Validation'], ['derivatives', 'Derivatives Data'], ['legacy', 'Legacy daily & crypto research'],
] as const;
type Tab = (typeof TABS)[number][0];

const blankDraft = (): Draft => ({ definition: emptyDefinition(), sourceText: null, editing: null });

export function SignalsWorkspace() {
  const [tab, setTab] = useState<Tab>('dashboard');
  const [meta, setMeta] = useState<Meta | null>(null);
  const [strategies, setStrategies] = useState<StrategyRow[]>([]);
  const [instruments, setInstruments] = useState<InstrumentRow[]>([]);
  const [health, setHealth] = useState<Health | null>(null);
  const [jobsRun, setJobsRun] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [draft, setDraft] = useState<Draft>(blankDraft);
  const [notice, setNotice] = useState<string | null>(null);
  const [btStrategy, setBtStrategy] = useState<string | null>(null);
  const [readiness, setReadiness] = useState<Readiness | null>(null);

  const reloadStrategies = useCallback(async () => {
    try { setStrategies(await lab<StrategyRow[]>('/strategies')); setError(null); } catch (e) { setError(messageOf(e)); }
  }, []);
  const reloadHealth = useCallback(async () => {
    try { setHealth(await lab<Health>('/health')); } catch (e) { setError(messageOf(e)); }
  }, []);

  useEffect(() => {
    let live = true;
    (async () => {
      try {
        // Starter strategies are no longer auto-installed on load; the library is exactly what the owner has created.
        const [m, s, i] = await Promise.all([lab<Meta>('/meta'), lab<StrategyRow[]>('/strategies'), lab<InstrumentRow[]>('/instruments')]);
        if (!live) return;
        setMeta(m); setStrategies(s); setInstruments(i);
        lab<{ readiness: Readiness }>('/starters').then((r) => live && setReadiness(r.readiness)).catch(() => undefined);
        void reloadHealth();
      } catch (e) { if (live) setError(messageOf(e)); } finally { if (live) setLoading(false); }
    })();
    return () => { live = false; };
  }, [reloadHealth]);

  const refreshAll = useCallback(() => { void reloadStrategies(); void reloadHealth(); }, [reloadStrategies, reloadHealth]);

  const editStrategy = async (s: StrategyRow) => {
    try {
      const full = await lab<StrategyRow>(`/strategies/${s.id}`);
      const v = full.versions?.find((x) => x.version === full.current_version);
      if (!v?.definition) { setNotice('This is a legacy daily strategy. Use “Convert to intraday draft”.'); return; }
      setDraft({ definition: v.definition, sourceText: null, editing: { id: s.id, name: s.name, version: full.current_version } });
      setTab('builder');
      setNotice(null);
    } catch (e) { setNotice(messageOf(e)); }
  };
  const openStarter = async (s: StrategyRow) => {
    try {
      const full = await lab<StrategyRow>(`/strategies/${s.id}`);
      const v = full.versions?.find((x) => x.version === full.current_version);
      if (!v?.definition) return;
      const base = s.name.replace(/^Starter: /, '');
      const definition: Definition = {
        ...v.definition, name: `${base} (custom)`.slice(0, 120),
        source: { type: 'visual', artifact: null, provenance: { derived_from: { strategy_id: s.id, version: full.current_version, starter_key: s.starter_key ?? null, name: s.name } } },
      };
      setDraft({ definition, sourceText: null, editing: null });
      setNotice(`Opened an editable copy of ${s.name}. Choose instruments, adjust anything, then save as a new strategy. The starter itself is never changed.`);
      setTab('builder');
    } catch (e) { setNotice(messageOf(e)); }
  };
  const openDraft = (definition: Definition, text: string | null, note: string) => {
    setDraft({ definition, sourceText: text, editing: null });
    setNotice(note);
    setTab('builder');
  };
  const useIndicator = (id: string) => {
    if (!meta) return;
    const info = meta.indicators.find((i) => i.id === id);
    if (!info) return;
    const base = draft.definition;
    setDraft({ ...draft, definition: { ...base, indicators: [...base.indicators, { alias: nextAlias(id, base.indicators.map((x) => x.alias)), indicator: id, params: defaultParams(info), timeframe: base.decision_timeframe }] } });
    setNotice(`${info.name} added to the builder draft.`);
    setTab('builder');
  };

  return (
    <section className="sl-root" aria-label="Signals workspace">
      <header className="sl-head">
        <div><p className="eyebrow">INTRADAY · 1-MINUTE BASE DATA</p><h1>Signals</h1></div>
        <p className="sl-muted">Strategy research, backtesting and signal monitoring for Indian equities and crypto. Decision support only: nothing here places orders.</p>
      </header>
      <nav className="sl-tabs" aria-label="Signals sections">
        {TABS.map(([key, label]) => <button key={key} type="button" className={tab === key ? 'active' : ''} aria-current={tab === key ? 'page' : undefined} onClick={() => { setTab(key); if (key !== 'builder') setNotice(null); }}>{label}</button>)}
      </nav>
      {notice && tab === 'builder' && <Notice tone="info">{notice}</Notice>}
      {error && !meta && <Notice tone="bad">{error}</Notice>}
      {tab === 'legacy' ? <Strategies /> : !meta ? <p className="sl-muted">{loading ? 'Loading Signals workspace…' : 'The Signals workspace could not be loaded.'}</p> : (
        <>
          {tab === 'dashboard' && <Dashboard health={health} loading={loading} error={error} goto={(t) => setTab(t as Tab)} reload={() => void reloadHealth()} />}
          {tab === 'builder' && <Builder meta={meta} draft={draft} setDraft={setDraft} onReset={() => { setDraft(blankDraft()); setNotice(null); }}
            onSaved={(_s, message) => { setDraft(blankDraft()); refreshAll(); setTab('library'); setNotice(message); }} />}
          {tab === 'library' && (
            <>
              {notice && <Notice tone="ok">{notice}</Notice>}
              <Library strategies={strategies} loading={loading} error={error} readiness={readiness} onReload={refreshAll} onEdit={editStrategy} onOpenStarter={openStarter} onDraft={openDraft} onBacktest={(s) => { setBtStrategy(s.id); setTab('backtesting'); }} />
            </>
          )}
          {tab === 'backtesting' && <Backtesting meta={meta} strategies={strategies} instruments={instruments} initialStrategyId={btStrategy} onUsed={refreshAll} onStoredChanged={refreshAll} />}
          {tab === 'jobs' && (
            <div className="sl-stack">
              <ScannerSchedulePanel strategies={strategies} />
              <ScanHistoryPanel onShowRejections={(id) => setJobsRun(id)} />
              {jobsRun && <RejectionsPanel strategies={strategies} instruments={instruments} scanRunId={jobsRun} />}
              <JobsPanel />
            </div>
          )}
          {tab === 'live' && <LiveSignals strategies={strategies} instruments={instruments} onChanged={refreshAll} />}
          {tab === 'indicators' && <IndicatorLibrary meta={meta} onUse={useIndicator} />}
          {tab === 'import' && <ImportPanel meta={meta} onDraft={openDraft} />}
          {tab === 'derivatives' && <DerivativesData />}
        </>
      )}
    </section>
  );
}
