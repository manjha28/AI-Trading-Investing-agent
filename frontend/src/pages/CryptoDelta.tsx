import { useEffect, useState } from 'react';
import { CryptoExecution } from '../crypto/CryptoExecution';
import { CryptoForward } from '../crypto/CryptoForward';
import { CryptoLiveReport } from '../crypto/CryptoLiveReport';
import { CryptoLab } from '../crypto/CryptoLab';
import { CryptoMarkets } from '../crypto/CryptoMarkets';
import { DeltaResearchPage } from './DeltaResearch';

type Tab = 'markets' | 'strategies' | 'live' | 'system';
const TABS: { id: Tab; label: string }[] = [
  { id: 'markets', label: 'Markets' },
  { id: 'strategies', label: 'Strategies' },
  { id: 'live', label: 'Live trading' },
  { id: 'system', label: 'System' },
];

function remembered<T extends string>(key: string, fallback: T, allowed?: readonly string[]): T {
  try {
    const value = window.localStorage.getItem(key);
    if (value && (!allowed || allowed.includes(value))) return value as T;
  } catch { /* storage unavailable: use the default */ }
  return fallback;
}

/**
 * Delta Exchange India crypto perpetuals: a self-contained workspace, separate from the Indian-equity
 * system (own candles, strategies, backtests). Whether it can send real orders is the backend's effective execution state, shown on the page.
 */
export function CryptoDeltaPage() {
  const [tab, setTab] = useState<Tab>(() => remembered<Tab>('crypto.tab', 'markets', TABS.map((t) => t.id)));
  const [view, setView] = useState<'tests' | 'backtests'>(() => remembered<'tests' | 'backtests'>('crypto.strategies', 'tests', ['tests', 'backtests']));
  const [symbol, setSymbol] = useState<string>(() => remembered<string>('crypto.symbol', 'BTCUSD'));
  const [resolution, setResolution] = useState<string>(() => remembered<string>('crypto.resolution', '15m', ['1m', '5m', '15m', '1h', '4h', '1d']));

  useEffect(() => {
    try {
      window.localStorage.setItem('crypto.tab', tab);
      window.localStorage.setItem('crypto.strategies', view);
      window.localStorage.setItem('crypto.symbol', symbol);
      window.localStorage.setItem('crypto.resolution', resolution);
    } catch { /* per-viewer convenience only */ }
  }, [tab, view, symbol, resolution]);

  return (
    <main className="crypto-page">
      <div className="crypto-intro"><h2>Crypto · Delta</h2></div>
      <nav className="crypto-tabs" role="tablist" aria-label="Crypto workspace">
        {TABS.map((item) => (
          <button key={item.id} type="button" role="tab" aria-selected={tab === item.id} className={`${tab === item.id ? 'active' : ''}${item.id === 'system' ? ' crypto-tab-secondary' : ''}`} onClick={() => setTab(item.id)}>{item.label}</button>
        ))}
      </nav>
      {tab === 'markets' && <CryptoMarkets symbol={symbol} setSymbol={setSymbol} resolution={resolution} setResolution={setResolution} />}
      {tab === 'strategies' && (
        <>
          <div className="crypto-subtabs" role="tablist" aria-label="Strategies">
            <button type="button" role="tab" aria-selected={view === 'tests'} className={view === 'tests' ? 'active' : ''} onClick={() => setView('tests')}>Paper tests &amp; scanners</button>
            <button type="button" role="tab" aria-selected={view === 'backtests'} className={view === 'backtests' ? 'active' : ''} onClick={() => setView('backtests')}>Backtests</button>
          </div>
          {view === 'backtests' ? <CryptoLab symbol={symbol} setSymbol={setSymbol} resolution={resolution} setResolution={setResolution} /> : <CryptoForward symbol={symbol} resolution={resolution} part="tests" />}
        </>
      )}
      {tab === 'live' && (
        <>
          <CryptoForward symbol={symbol} resolution={resolution} part="live" />
          <CryptoLiveReport />
        </>
      )}
      {tab === 'system' && (
        <>
          <details className="crypto-details" open><summary>Feed collection</summary><DeltaResearchPage /></details>
          <details className="crypto-details"><summary>Execution details and developer tools (readiness, risk-check sandbox, audit trail)</summary><CryptoExecution /></details>
        </>
      )}
    </main>
  );
}
