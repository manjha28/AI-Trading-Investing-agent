import { useCallback, useEffect, useMemo, useState } from 'react';
import { apiRequest } from '../api';
import { CryptoCandleChart } from './CryptoCharts';
import { CryptoWatch } from './CryptoWatch';
import { DEFAULT_DAYS, RESOLUTIONS, atTick, messageOf, type Candles, type ContractInfo, type Market } from './cryptoApi';

type Props = { symbol: string; setSymbol: (symbol: string) => void; resolution: string; setResolution: (resolution: string) => void };

function shortNumber(value: string | null): string {
  if (value === null) return '—';
  const n = Number(value);
  if (!Number.isFinite(n)) return '—';
  return n >= 1e9 ? `${(n / 1e9).toFixed(2)}B` : n >= 1e6 ? `${(n / 1e6).toFixed(2)}M` : n >= 1e3 ? `${(n / 1e3).toFixed(1)}K` : n.toFixed(2);
}

export function CryptoMarkets({ symbol, setSymbol, resolution, setResolution }: Props) {
  const [markets, setMarkets] = useState<Market[]>([]);
  const [marketsError, setMarketsError] = useState<string | null>(null);
  const [marketsLoading, setMarketsLoading] = useState(true);       // true until the first response or error: never say "No contracts match" while the request is in flight
  const [chartLoading, setChartLoading] = useState(true);
    const [candles, setCandles] = useState<Candles | null>(null);
  const [contract, setContract] = useState<ContractInfo | null>(null);
  const [chartError, setChartError] = useState<string | null>(null);
  const [syncing, setSyncing] = useState(false);
  const [syncNote, setSyncNote] = useState<string | null>(null);

  const loadMarkets = useCallback(async () => {
    try {
      const result = await apiRequest<{ items: Market[] }>('/delta-research/markets?limit=500&slim=true');
      setMarkets(result.items);
      setMarketsError(null);
    } catch (error) {
      setMarketsError(messageOf(error));
    } finally {
      setMarketsLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadMarkets();
    const timer = window.setInterval(() => void loadMarkets(), 15_000);
    return () => window.clearInterval(timer);
  }, [loadMarkets]);

  const loadChart = useCallback(async () => {
    setChartError(null);
    setChartLoading(true);
    const [c, info] = await Promise.allSettled([
      apiRequest<Candles>(`/crypto-delta/candles?symbol=${encodeURIComponent(symbol)}&resolution=${resolution}&limit=500`),
      apiRequest<ContractInfo>(`/crypto-delta/contracts/${encodeURIComponent(symbol)}`),
    ]);
    if (c.status === 'fulfilled') setCandles(c.value); else { setCandles(null); setChartError(messageOf(c.reason)); }
    setContract(info.status === 'fulfilled' ? info.value : null);
    setChartLoading(false);
  }, [symbol, resolution]);

  useEffect(() => { setSyncNote(null); void loadChart(); }, [loadChart]);

  async function download() {
    setSyncing(true);
    setSyncNote(null);
    try {
      const report = await apiRequest<{ fetched: number; inserted: number; incomplete_skipped: number }>('/crypto-delta/candles/sync', {
        method: 'POST',
        body: JSON.stringify({ symbol, resolution, days: DEFAULT_DAYS[resolution] }),
      });
      setSyncNote(`Downloaded ${report.fetched} candles from Delta; ${report.inserted} new, ${report.incomplete_skipped} still forming (not stored).`);
      await loadChart();
    } catch (error) {
      setSyncNote(messageOf(error));
    } finally {
      setSyncing(false);
    }
  }

  const selected = markets.find((m) => m.symbol === symbol);

  return (
    <div className="crypto-stack">
      <CryptoWatch symbol={symbol} setSymbol={setSymbol} />

      <section className="data-section crypto-chart-panel" aria-label="Chart">
        <div className="section-heading">
          <div><p className="eyebrow">CHART · COMPLETED CANDLES</p><h3>{symbol}{selected?.mark_price ? <span className="crypto-mark"> mark {atTick(selected.mark_price, contract?.tick_size)}</span> : null}</h3></div>
          <div className="crypto-resolutions" role="group" aria-label="Candle size">
            {RESOLUTIONS.map((r) => <button key={r} type="button" className={r === resolution ? 'active' : ''} aria-pressed={r === resolution} onClick={() => setResolution(r)}>{r}</button>)}
          </div>
        </div>
        {chartError && <p className="error-message" role="alert">{chartError}</p>}
        {candles && candles.count > 0
          ? <CryptoCandleChart candles={candles.candles} />
          : chartLoading ? <div className="table-empty" role="status" aria-busy="true">Loading {resolution} candles…</div>
          : !chartError && <div className="table-empty">No {resolution} candles stored for {symbol}. <button className="primary-button" type="button" onClick={() => void download()} disabled={syncing}>{syncing ? 'Downloading…' : `Download ${DEFAULT_DAYS[resolution]}d of ${resolution} candles`}</button></div>}
        <div className="crypto-chart-actions">
          <button className="primary-button" type="button" onClick={() => void download()} disabled={syncing}>{syncing ? 'Downloading…' : `Download ${DEFAULT_DAYS[resolution]}d of ${resolution} candles`}</button>
          <span className="crypto-sub">
            {candles ? `${candles.count} candles shown · ${candles.gaps} gap${candles.gaps === 1 ? '' : 's'} · source: Delta public history (completed candles only)` : ''}
          </span>
        </div>
        {syncNote && <p className="alerts-notice" role="status">{syncNote}</p>}
        {contract && (
          <div className="crypto-contract" aria-label="Contract terms">
            <div><span>Contract value</span><strong>{contract.contract_value}</strong></div>
            <div><span>Tick size</span><strong>{contract.tick_size ?? '—'}</strong></div>
            <div><span>Taker fee</span><strong>{(Number(contract.taker_fee_rate) * 100).toFixed(3)}%</strong></div>
            <div><span>Maker fee</span><strong>{(Number(contract.maker_fee_rate) * 100).toFixed(3)}%</strong></div>
            <div><span>Quote</span><strong>{contract.quote_currency}</strong></div>
          </div>
        )}
      </section>
    </div>
  );
}
