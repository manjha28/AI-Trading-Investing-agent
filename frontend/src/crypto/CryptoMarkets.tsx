import { useCallback, useEffect, useMemo, useState } from 'react';
import { apiRequest } from '../api';
import { CryptoCandleChart } from './CryptoCharts';
import { FeedToggle } from './FeedToggle';
import { DEFAULT_DAYS, RESOLUTIONS, atTick, messageOf, type Candles, type ContractInfo, type Market } from './cryptoApi';

type Props = { symbol: string; setSymbol: (symbol: string) => void; resolution: string; setResolution: (resolution: string) => void };

type SortKey = 'symbol' | 'last' | 'change' | 'volume' | 'oi' | 'funding';
const num = (value: string | null | undefined): number | null => { if (value === null || value === undefined || value === '') return null; const n = Number(value); return Number.isFinite(n) ? n : null; };
const plain = (value: string | null | undefined): string => { const n = num(value); return n === null ? '—' : n.toLocaleString('en-US', { maximumFractionDigits: n >= 100 ? 2 : n >= 1 ? 4 : 8 }); };
const tone = (value: string | null | undefined): string => { const n = num(value); return n === null || n === 0 ? '' : n > 0 ? 'positive' : 'negative'; };
const signedShort = (value: string | null | undefined): string => { const n = num(value); return n === null ? '—' : `${n > 0 ? '▲ +' : n < 0 ? '▼ -' : ''}${shortNumber(String(Math.abs(n)))}`; };

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
  const [query, setQuery] = useState('');
  const [candles, setCandles] = useState<Candles | null>(null);
  const [contract, setContract] = useState<ContractInfo | null>(null);
  const [chartError, setChartError] = useState<string | null>(null);
  const [syncing, setSyncing] = useState(false);
  const [syncNote, setSyncNote] = useState<string | null>(null);

  const loadMarkets = useCallback(async () => {
    try {
      const result = await apiRequest<{ items: Market[] }>('/delta-research/markets?limit=500');
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

  const [tab, setTab] = useState<'all' | 'watchlist' | 'gainers' | 'losers'>('all');
  const [sort, setSort] = useState<{ key: SortKey; dir: 1 | -1 }>({ key: 'volume', dir: -1 });
  const [starred, setStarred] = useState<string[]>(() => { try { return JSON.parse(window.localStorage.getItem('crypto.watchlist') ?? '[]') as string[]; } catch { return []; } });
  const toggleStar = (sym: string) => setStarred((cur) => {
    const next = cur.includes(sym) ? cur.filter((x) => x !== sym) : [...cur, sym];
    try { window.localStorage.setItem('crypto.watchlist', JSON.stringify(next)); } catch { /* per-viewer convenience only */ }
    return next;
  });

  const rows = useMemo(() => {
    const term = query.trim().toUpperCase();
    const out = markets.filter((m) => m.state === 'live' && (!term || m.symbol.toUpperCase().includes(term)) && (tab !== 'watchlist' || starred.includes(m.symbol)));
    const value = (m: Market): number | null => {
      const t = m.ticker;
      switch (sort.key) {
        case 'symbol': return null;
        case 'last': return num(m.last_price);
        case 'change': return num(t?.ltp_change_24h);
        case 'volume': return num(t?.turnover_usd ?? m.volume_24h);
        case 'oi': return num(t?.oi_value_usd);
        case 'funding': return num(t?.funding_rate);
      }
    };
    const key = tab === 'gainers' ? { key: 'change' as SortKey, dir: -1 as const } : tab === 'losers' ? { key: 'change' as SortKey, dir: 1 as const } : sort;
    const pick = (m: Market) => (key.key === 'change' ? num(m.ticker?.ltp_change_24h) : value(m));
    return [...out].sort((x, y) => {
      if (key.key === 'symbol') return x.symbol.localeCompare(y.symbol) * key.dir;
      const a = pick(x); const b = pick(y);
      if (a === null && b === null) return 0;
      if (a === null) return 1;          // unknown values sort last, never as zero
      if (b === null) return -1;
      return (a - b) * key.dir;
    });
  }, [markets, query, tab, sort, starred]);
  const sortBy = (key: SortKey) => setSort((cur) => ({ key, dir: cur.key === key ? (cur.dir === 1 ? -1 : 1) : key === 'symbol' ? 1 : -1 }));
  const arrow = (key: SortKey) => (sort.key === key ? (sort.dir === 1 ? ' ▲' : ' ▼') : '');
  const selected = markets.find((m) => m.symbol === symbol);
  const newest = markets.reduce((latest, m) => (m.quote_time && m.quote_time > latest ? m.quote_time : latest), '');
  const age = newest ? Math.max(0, Math.round((Date.now() - new Date(newest).getTime()) / 1000)) : null;

  return (
    <div className="crypto-stack">
      <section className="data-section crypto-watch" aria-label="Perpetual contracts">
        <div className="section-heading">
          <div><p className="eyebrow">WATCH</p><h3>Perpetuals</h3></div>
          <span className="section-count">{rows.length} of {markets.filter((m) => m.state === 'live').length}</span>
          <FeedToggle />
        </div>
        <div className="watch-toolbar">
          <input type="search" placeholder="Search contract, e.g. SOL" value={query} onChange={(event) => setQuery(event.target.value)} aria-label="Search contracts" />
          <div className="crypto-resolutions" role="tablist" aria-label="Watch filter">
            {([['all', 'All'], ['watchlist', `Watchlist (${starred.length})`], ['gainers', 'Gainers'], ['losers', 'Losers']] as const).map(([id, label]) => (
              <button key={id} type="button" role="tab" aria-selected={tab === id} className={tab === id ? 'active' : ''} onClick={() => setTab(id)}>{label}</button>
            ))}
          </div>
        </div>
        {marketsError && <p className="error-message" role="alert">{marketsError}</p>}
        <div className="watch-scroll">
          <table className="watch-table">
            <thead><tr>
              <th aria-label="Watchlist" />
              <th><button type="button" onClick={() => sortBy('symbol')}>Contract{arrow('symbol')}</button></th>
              <th className="watch-num"><button type="button" onClick={() => sortBy('last')}>Last{arrow('last')}</button></th>
              <th className="watch-num"><button type="button" onClick={() => sortBy('change')}>24h %{arrow('change')}</button></th>
              <th>24h range</th>
              <th className="watch-num"><button type="button" onClick={() => sortBy('volume')}>Volume (USD){arrow('volume')}</button></th>
              <th className="watch-num"><button type="button" onClick={() => sortBy('oi')}>Open interest (USD){arrow('oi')}</button></th>
              <th className="watch-num">OI 6h</th>
              <th className="watch-num" title="Funding rate as reported by Delta"><button type="button" onClick={() => sortBy('funding')}>Funding{arrow('funding')}</button></th>
              <th className="watch-num">Mark</th>
              <th className="watch-num">Bid / Ask</th>
            </tr></thead>
            <tbody>
              {rows.map((m) => {
                const t = m.ticker;
                const chg = num(t?.ltp_change_24h);
                return (
                  <tr key={m.product_id} className={m.symbol === symbol ? 'selected' : ''} onClick={() => setSymbol(m.symbol)} aria-selected={m.symbol === symbol}>
                    <td><button type="button" className="star-button" aria-label={`${starred.includes(m.symbol) ? 'Remove' : 'Add'} ${m.symbol} ${starred.includes(m.symbol) ? 'from' : 'to'} watchlist`} aria-pressed={starred.includes(m.symbol)} onClick={(e) => { e.stopPropagation(); toggleStar(m.symbol); }}>{starred.includes(m.symbol) ? '★' : '☆'}</button></td>
                    <td><button type="button" className="link-button" onClick={() => setSymbol(m.symbol)}><strong>{m.symbol}</strong></button>{t?.leverage ? <small className="crypto-sub"> up to {t.leverage}x</small> : null}</td>
                    <td className="watch-num">{plain(m.last_price)}</td>
                    <td className={`watch-num ${chg === null ? '' : chg > 0 ? 'positive' : chg < 0 ? 'negative' : ''}`}>{chg === null ? '—' : `${chg > 0 ? '▲ +' : chg < 0 ? '▼ ' : ''}${chg.toFixed(2)}%`}</td>
                    <td>{t?.low && t?.high ? `${plain(t.low)} – ${plain(t.high)}` : '—'}</td>
                    <td className="watch-num">{shortNumber(t?.turnover_usd ?? m.volume_24h)}</td>
                    <td className="watch-num">{shortNumber(t?.oi_value_usd ?? null)}</td>
                    <td className={`watch-num ${tone(t?.oi_change_usd_6h)}`}>{signedShort(t?.oi_change_usd_6h)}</td>
                    <td className={`watch-num ${tone(t?.funding_rate)}`}>{num(t?.funding_rate) === null ? '—' : `${num(t?.funding_rate)!.toFixed(4)}%`}</td>
                    <td className="watch-num">{plain(m.mark_price)}</td>
                    <td className="watch-num">{t?.best_bid && t?.best_ask ? `${plain(t.best_bid)} / ${plain(t.best_ask)}` : '—'}</td>
                  </tr>
                );
              })}
              {rows.length === 0 && marketsLoading && <tr><td colSpan={11} className="table-empty" role="status" aria-busy="true">Loading contracts…</td></tr>}
              {rows.length === 0 && !marketsLoading && !marketsError && <tr><td colSpan={11} className="table-empty">{tab === 'watchlist' ? 'No starred contracts yet. Tap ☆ on a row to add it.' : 'No live contracts match. The public feed may be paused or still starting.'}</td></tr>}
            </tbody>
          </table>
        </div>
        <p className="crypto-sub">{age === null ? 'No quotes stored yet.' : `Newest quote ${age}s old${age > 120 ? ' · feed paused or stale, showing the last stored prices' : ''}`}. Delta public data, refreshed every 15 seconds. Unknown values show —.</p>
      </section>

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
          : !chartError && <div className="table-empty">No {resolution} candles stored for {symbol}. Download them from Delta's public history to chart and backtest.</div>}
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
