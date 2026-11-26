import { useCallback, useEffect, useState } from 'react';
import { Backtesting } from './Backtesting';
import { BacktestLab } from './BacktestLab';
import { Builder, type Draft } from './Builder';
import { Dashboard } from './Dashboard';
import { ImportPanel } from './ImportPanel';
import { IndicatorLibrary } from './IndicatorLibrary';
import { Library } from './Library';
import { JobsPanel, ScanHistoryPanel, ScannerSchedulePanel } from './JobsScanner';
import { LiveSignals } from './LiveSignals';
import { RejectionsPanel } from './RejectionsPanel';
import { ForwardTest } from './ForwardTest';
import { StrategyRegistry } from './StrategyRegistry';
import { defaultParams, emptyDefinition, lab, messageOf, nextAlias } from './lab';
import { Notice } from './shared';
import { ExecutionStateChip } from '../components/ExecutionStateChip';
import { useExecutionState } from '../hooks/useExecutionState';
import type { Definition, Health, InstrumentRow, Meta, Readiness, StrategyRow } from './types';
import './signals.css';
import { useSlow } from '../hooks/useSlow';

// The workbench: build strategies from every indicator, keep a library of code-defined ones, and backtest many strategies over many symbols.
// (VAR research, derivatives data and the legacy daily page are no longer part of this page; their code is untouched.)
const TABS = [
  ['dashboard', 'Strategy Dashboard'], ['library', 'Strategy Library'], ['registry', 'Strategy Registry'], ['builder', 'Strategy Builder'], ['lab', 'Compare strategies'],
  ['backtesting', 'Test one strategy'], ['forward', 'Forward Test'], ['live', 'Live Signals'], ['jobs', 'Jobs & scanner'], ['indicators', 'Indicator Library'], ['import', 'Import & Validation'],
] as const;
type Tab = (typeof TABS)[number][0];
// Two levels: six primary sections, with the detailed tabs inside the section that owns them. Every tab stays reachable.
const GROUPS: { id: string; label: string; tabs: Tab[] }[] = [
  { id: 'dashboard', label: 'Dashboard', tabs: ['dashboard'] },
  { id: 'strategies', label: 'Strategies', tabs: ['library', 'registry', 'builder', 'indicators', 'import'] },
  { id: 'backtest', label: 'Backtest', tabs: ['lab', 'backtesting'] },
  { id: 'forward', label: 'Forward test', tabs: ['forward'] },
  { id: 'live', label: 'Live signals', tabs: ['live'] },
  { id: 'jobs', label: 'Jobs', tabs: ['jobs'] },
];
const labelOf = (t: Tab) => TABS.find(([k]) => k === t)?.[1] ?? t;

const blankDraft = (): Draft => ({ definition: emptyDefinition(), sourceText: null, editing: null });

export function SignalsWorkspace() {
  const [tab, setTabRaw] = useState<Tab>('dashboard');
  const [lastInGroup, setLastInGroup] = useState<Record<string, Tab>>({});
  const executionStates = useExecutionState();
  const group = GROUPS.find((g) => g.tabs.includes(tab)) ?? GROUPS[0];
  const setTab = useCallback((t: Tab) => { setTabRaw(t); const g = GROUPS.find((x) => x.tabs.includes(t)); if (g) setLastInGroup((cur) => ({ ...cur, [g.id]: t })); }, []);
  const [meta, setMeta] = useState<Meta | null>(null);
  const [strategies, setStrategies] = useState<StrategyRow[]>([]);
  const [instruments, setInstruments] = useState<InstrumentRow[]>([]);
  const [health, setHealth] = useState<Health | null>(null);
  const [jobsRun, setJobsRun] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const slowWorkspace = useSlow(loading);
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
        <p className="sl-muted">Strategy research, backtesting and signal monitoring for Indian equities. Decision support only: nothing here places orders.</p>
      </header>
      <nav className="sl-tabs" aria-label="Signals sections">
        {GROUPS.map((g) => <button key={g.id} type="button" className={group.id === g.id ? 'active' : ''} aria-current={group.id === g.id ? 'page' : undefined} onClick={() => { if (group.id !== g.id) setTab(lastInGroup[g.id] ?? g.tabs[0]); setNotice(null); }}>{g.label}</button>)}
      </nav>
      {group.tabs.length > 1 && (
        <nav className="sl-tabs sl-subtabs" aria-label={`${group.label} sections`}>
          {group.tabs.map((key) => <button key={key} type="button" className={tab === key ? 'active' : ''} aria-current={tab === key ? 'page' : undefined} onClick={() => { setTab(key); if (key !== 'builder') setNotice(null); }}>{labelOf(key)}</button>)}
        </nav>
      )}
      {(tab === 'forward' || tab === 'live') && <p className="sl-capability" aria-label="Live execution state"><ExecutionStateChip venue={executionStates?.dhan} /> <ExecutionStateChip venue={executionStates?.delta} /></p>}
      {notice && tab === 'builder' && <Notice tone="info">{notice}</Notice>}
      {error && !meta && <Notice tone="bad">{error}</Notice>}
      {!meta ? <p className="sl-muted">{loading ? (slowWorkspace ? 'Still loading (slow backend). This can take up to a minute…' : 'Loading Signals workspace…') : (error ?? 'The Signals workspace could not be loaded.')}</p> : (
        <>
          {tab === 'dashboard' && <Dashboard health={health} loading={loading} error={error} goto={(t) => setTab(t as Tab)} reload={() => void reloadHealth()} />}
          {tab === 'builder' && <Builder meta={meta} draft={draft} setDraft={setDraft} onReset={() => { setDraft(blankDraft()); setNotice(null); }}
            onSaved={(_s, message) => { setDraft(blankDraft()); refreshAll(); setTab('library'); setNotice(message); }} />}
          {tab === 'library' && (
            <>
              {notice && <Notice tone="ok">{notice}</Notice>}
              <Library onRegistry={() => setTab('registry')} onBuild={() => { setDraft(blankDraft()); setTab('builder'); }} strategies={strategies} loading={loading} error={error} readiness={readiness} onReload={refreshAll} onEdit={editStrategy} onOpenStarter={openStarter} onDraft={openDraft} onBacktest={(s) => { setBtStrategy(s.id); setTab('backtesting'); }} />
            </>
          )}
          {tab === 'backtesting' && <Backtesting meta={meta} strategies={strategies} instruments={instruments} initialStrategyId={btStrategy} onUsed={refreshAll} onStoredChanged={refreshAll} />}
          {tab === 'registry' && <StrategyRegistry strategies={strategies} onInstalled={refreshAll} onOpen={openStarter} goLab={() => setTab('lab')} />}
          {tab === 'forward' && <ForwardTest />}
          {tab === 'lab' && <BacktestLab meta={meta} strategies={strategies} />}
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
        </>
      )}
    </section>
  );
}
