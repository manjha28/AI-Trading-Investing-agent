import { useEffect, useState } from 'react';
import { CryptoExecution } from '../crypto/CryptoExecution';
import { CryptoLab } from '../crypto/CryptoLab';
import { CryptoMarkets } from '../crypto/CryptoMarkets';
import { DeltaResearchPage } from './DeltaResearch';

type Tab = 'markets' | 'lab' | 'feed' | 'execution';
const TABS: { id: Tab; label: string }[] = [
  { id: 'markets', label: 'Markets & charts' },
  { id: 'lab', label: 'Strategy lab' },
  { id: 'feed', label: 'Live feed' },
  { id: 'execution', label: 'Execution' },
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
 * system (own candles, strategies, backtests). Research only; no order is placed or transmitted.
 */
export function CryptoDeltaPage() {
  const [tab, setTab] = useState<Tab>(() => remembered<Tab>('crypto.tab', 'markets', TABS.map((t) => t.id)));
  const [symbol, setSymbol] = useState<string>(() => remembered<string>('crypto.symbol', 'BTCUSD'));
  const [resolution, setResolution] = useState<string>(() => remembered<string>('crypto.resolution', '15m', ['1m', '5m', '15m', '1h', '4h', '1d']));

  useEffect(() => {
    try {
      window.localStorage.setItem('crypto.tab', tab);
      window.localStorage.setItem('crypto.symbol', symbol);
      window.localStorage.setItem('crypto.resolution', resolution);
    } catch { /* per-viewer convenience only */ }
  }, [tab, symbol, resolution]);

  return (
    <main className="crypto-page">
      <div className="crypto-intro">
        <div>
          <p className="eyebrow">DELTA EXCHANGE INDIA · CRYPTO PERPETUALS</p>
          <h2>Perpetuals workspace</h2>
          <p className="crypto-sub">Watch, chart and backtest perpetual contracts. Separate from the Indian-equity system; research only. Decision support, not investment advice.</p>
        </div>
      </div>
      <nav className="crypto-tabs" role="tablist" aria-label="Crypto workspace">
        {TABS.map((item) => (
          <button key={item.id} type="button" role="tab" aria-selected={tab === item.id} className={tab === item.id ? 'active' : ''} onClick={() => setTab(item.id)}>{item.label}</button>
        ))}
      </nav>
      {tab === 'markets' && <CryptoMarkets symbol={symbol} setSymbol={setSymbol} resolution={resolution} setResolution={setResolution} />}
      {tab === 'lab' && <CryptoLab symbol={symbol} setSymbol={setSymbol} resolution={resolution} setResolution={setResolution} />}
      {tab === 'feed' && <DeltaResearchPage />}
      {tab === 'execution' && <CryptoExecution />}
    </main>
  );
}
