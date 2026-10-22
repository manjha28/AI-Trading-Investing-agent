import {
  ArrowUpRight, CircleDollarSign, Clock3, Pencil, Plus, Search, ShieldCheck, Trash2, Wallet,
} from 'lucide-react';
import { useEffect, useState } from 'react';
import { apiRequest } from '../api';
import { CandleChart } from '../components/CandleChart';
import { ThesisEditor } from '../components/ThesisEditor';
import {
  formatMoney, formatQuantity, localDateTime, marketRangeParams, indicatorLabels,
  type WorkspaceState,
} from '../components/useWorkspace';

type FundamentalPeriod = {
  id: string;
  period_type: 'annual' | 'quarterly';
  period_end: string;
  currency: string;
  revenue: string | null;
  net_income: string | null;
  eps: string | null;
  operating_margin: string | null;
  total_assets: string | null;
  total_liabilities: string | null;
  total_equity: string | null;
  operating_cash_flow: string | null;
  free_cash_flow: string | null;
  total_debt: string | null;
  shares_outstanding: string | null;
  source: string;
  fetched_at: string;
  stale: boolean;
  revenue_yoy_growth_pct: string | null;
  net_income_yoy_growth_pct: string | null;
  eps_yoy_growth_pct: string | null;
  free_cash_flow_yoy_growth_pct: string | null;
};
type FundamentalsData = {
  instrument_id: string;
  symbol: string;
  currency: string;
  snapshot: {
    market_cap: string | null;
    pe: string | null;
    pb: string | null;
    dividend_yield: string | null;
    sector: string | null;
    industry: string | null;
    source: string;
    fetched_at: string;
    stale: boolean;
  } | null;
  fundamentals: FundamentalPeriod[];
  unavailable: boolean;
  message: string;
};
type FundamentalsSync = { status: 'available' | 'unavailable'; message: string };

export function MarketDataPage({ workspace }: { workspace: Pick<WorkspaceState, 'addInstrumentToWatchlist' | 'addSelectedInstrumentToWatchlist' | 'attachSelectedInstrument' | 'currency' | 'loadInstrumentIndicators' | 'loadMarketHistory' | 'loadMarketQuote' | 'loadingMarketHistory' | 'loadingMarketIndicators' | 'loadingMarketQuote' | 'marketEnd' | 'marketHistory' | 'marketIndicators' | 'marketInstruments' | 'marketInterval' | 'marketQuery' | 'marketQuote' | 'marketStart' | 'removeInstrumentFromWatchlist' | 'searchMarketInstruments' | 'searchingMarket' | 'selectMarketInstrument' | 'selectedInstrument' | 'setMarketEnd' | 'setMarketHistory' | 'setMarketInterval' | 'setMarketQuery' | 'setMarketStart' | 'summary' | 'valuation' | 'watchlist'> }) {
  const {
  addInstrumentToWatchlist,
  addSelectedInstrumentToWatchlist,
  attachSelectedInstrument,
  currency,
  loadInstrumentIndicators,
  loadMarketHistory,
  loadMarketQuote,
  loadingMarketHistory,
  loadingMarketIndicators,
  loadingMarketQuote,
  marketEnd,
  marketHistory,
  marketIndicators,
  marketInstruments,
  marketInterval,
  marketQuery,
  marketQuote,
  marketStart,
  removeInstrumentFromWatchlist,
  searchMarketInstruments,
  searchingMarket,
  selectMarketInstrument,
  selectedInstrument,
  setMarketEnd,
  setMarketHistory,
  setMarketInterval,
  setMarketQuery,
  setMarketStart,
  summary,
  valuation,
  watchlist
} = workspace;
  const [detailTab, setDetailTab] = useState<'indicators' | 'fundamentals'>('indicators');
  const [fundamentals, setFundamentals] = useState<FundamentalsData | null>(null);
  const [fundamentalsLoading, setFundamentalsLoading] = useState(false);
  const [fundamentalsSyncing, setFundamentalsSyncing] = useState(false);
  const [fundamentalsMessage, setFundamentalsMessage] = useState('');
  const [fundamentalPeriodType, setFundamentalPeriodType] = useState<'annual' | 'quarterly'>('annual');

  useEffect(() => {
    if (!selectedInstrument) {
      setFundamentals(null);
      return;
    }
    let active = true;
    setFundamentalsLoading(true);
    setFundamentalsMessage('');
    apiRequest<FundamentalsData>(`/market-data/instruments/${selectedInstrument.id}/fundamentals?period_type=${fundamentalPeriodType}`)
      .then((data) => {
        if (active) setFundamentals(data);
      })
      .catch((error: Error) => {
        if (active) setFundamentalsMessage(error.message);
      })
      .finally(() => {
        if (active) setFundamentalsLoading(false);
      });
    return () => {
      active = false;
    };
  }, [selectedInstrument?.id, fundamentalPeriodType]);

  async function syncFundamentals() {
    if (!selectedInstrument) return;
    setFundamentalsSyncing(true);
    setFundamentalsMessage('');
    try {
      const result = await apiRequest<FundamentalsSync>(
        `/market-data/instruments/${selectedInstrument.id}/fundamentals/sync`,
        { method: 'POST', body: '{}' },
      );
      setFundamentalsMessage(result.message);
      setFundamentals(await apiRequest<FundamentalsData>(
        `/market-data/instruments/${selectedInstrument.id}/fundamentals?period_type=${fundamentalPeriodType}`,
      ));
    } catch (error) {
      setFundamentalsMessage(error instanceof Error ? error.message : 'Fundamentals sync failed.');
    } finally {
      setFundamentalsSyncing(false);
    }
  }

  const displayFundamental = (value: string | null, suffix = '') =>
    value === null ? 'Unavailable' : `${Number(value).toLocaleString(undefined, { maximumFractionDigits: 4 })}${suffix}`;
  return (
              <section className="market-page">
                <div className="data-note"><CircleDollarSign size={17} /><span>Market data provenance and freshness are shown for each result. Provider mappings are not verified exchange listings unless explicitly marked; market data is not used for portfolio valuation.</span></div>
                <div className="content-grid market-grid">
                  <section className="data-section">
                    <div className="section-heading"><div><p className="eyebrow">INSTRUMENT MASTER</p><h2>Search instruments</h2></div><Search size={18} /></div>
                    <form className="market-search" onSubmit={searchMarketInstruments}>
                      <label>Symbol, name, or venue<input value={marketQuery} onChange={(event) => setMarketQuery(event.target.value)} minLength={1} maxLength={80} placeholder="AAPL, RELIANCE, BTCUSD" required /></label>
                      <button className="primary-button" type="submit" disabled={searchingMarket}><Search size={15} />{searchingMarket ? 'Searching…' : 'Search'}</button>
                    </form>
                    {marketInstruments.length === 0 ? <div className="table-empty">{searchingMarket ? 'Searching instrument master…' : 'Search available equities and crypto markets.'}</div> : (
                      <div className="market-instrument-list" aria-label="Instrument search results">
                        {marketInstruments.map((instrument) => {
                          const isWatchlisted = watchlist.some((item) => item.instrument_id === instrument.id);
                          return <div className="market-instrument-result" key={instrument.id}>
                            <button className={selectedInstrument?.id === instrument.id ? 'market-instrument selected' : 'market-instrument'} type="button" onClick={() => void selectMarketInstrument(instrument)}>
                              <span><strong>{instrument.symbol}</strong><small>{instrument.display_name}</small></span><span className="market-instrument-meta">{instrument.venue} · {instrument.currency}<small>{instrument.asset_class}</small></span>
                            </button>
                            <button className="secondary-button" type="button" onClick={() => void (isWatchlisted ? removeInstrumentFromWatchlist(instrument.id) : addInstrumentToWatchlist(instrument.id, instrument.symbol))}>{isWatchlisted ? 'Remove' : 'Add to watchlist'}</button>
                          </div>;
                        })}
                      </div>
                    )}
                  </section>
                  <section className="entry-section market-controls">
                    <div className="section-heading"><div><p className="eyebrow">HISTORICAL BARS</p><h2>{selectedInstrument ? `${selectedInstrument.symbol} · ${selectedInstrument.venue}` : 'Select an instrument'}</h2></div><Clock3 size={18} /></div>
                    {selectedInstrument ? <>
                      <div className="market-instrument-details">
                        <p>{selectedInstrument.display_name}</p>
                        <span>{selectedInstrument.asset_class.toUpperCase()} · {selectedInstrument.country} · {selectedInstrument.currency} · {selectedInstrument.timezone}</span>
                        {selectedInstrument.is_synthetic && <strong className="synthetic-badge">SYNTHETIC DEMO INSTRUMENT</strong>}
                        {selectedInstrument.relink_required && <strong className="relink-warning">Portfolio holding needs relinking to a real instrument.</strong>}
                        {selectedInstrument.expiry && <span>Expiry {selectedInstrument.expiry}{selectedInstrument.strike ? ` · ${selectedInstrument.option_type} ${selectedInstrument.strike}` : ''}{selectedInstrument.multiplier ? ` · multiplier ${selectedInstrument.multiplier}` : ''}</span>}
                        {selectedInstrument.provider_identifiers.map((mapping) => <span key={`${mapping.provider}-${mapping.provider_instrument_id}`}>Provider ID: {mapping.provider}:{mapping.provider_instrument_id} · {mapping.is_verified ? 'verified mapping' : 'unverified mapping'}</span>)}
                      </div>
                      {summary?.positions.some((position) => position.symbol === selectedInstrument.symbol) && selectedInstrument.currency === currency && <button className="secondary-button" type="button" onClick={() => void attachSelectedInstrument()}>Link to portfolio holding</button>}
                      {watchlist.some((item) => item.instrument_id === selectedInstrument.id)
                        ? <button className="secondary-button" type="button" onClick={() => void removeInstrumentFromWatchlist(selectedInstrument.id)}>Remove from refresh watchlist</button>
                        : <button className="secondary-button" type="button" onClick={() => void addSelectedInstrumentToWatchlist()}>Add to refresh watchlist</button>}
                      <button className="secondary-button" type="button" onClick={() => void loadMarketQuote(selectedInstrument, true)} disabled={loadingMarketQuote}>{loadingMarketQuote ? 'Loading quote…' : 'Refresh instrument price'}</button>
                      {marketQuote && <div className="market-quote" aria-live="polite">
                        <div className="section-heading"><div><p className="eyebrow">LATEST PRICE</p><h3>{formatMoney(marketQuote.bar.close, marketQuote.currency)}</h3></div><span className={`freshness-badge ${marketQuote.is_synthetic ? 'freshness-synthetic' : `freshness-${marketQuote.freshness}`}`}>{marketQuote.is_synthetic ? 'synthetic' : marketQuote.freshness}</span></div>
                        <p className="market-provenance">{marketQuote.freshness_message} Source: {marketQuote.source} · Retrieved: {new Date(marketQuote.retrieved_at).toLocaleString()} · {marketQuote.is_cached ? 'Cached' : 'Provider response'} · {marketQuote.is_synthetic ? 'Synthetic' : marketQuote.is_verified ? 'Verified' : 'Unverified'}</p>
                      </div>}
                      <div className="market-range">
                          <label>Interval<select value={marketInterval} onChange={(event) => { const interval = event.target.value as '1d' | '1h' | '15m'; setMarketInterval(interval); setMarketHistory(null); if (selectedInstrument) void loadInstrumentIndicators(selectedInstrument, interval); }}><option value="1d">1 day</option><option value="1h">1 hour</option><option value="15m">15 minutes</option></select></label>
                          <label>From<input type="date" value={marketStart} onChange={(event) => { setMarketStart(event.target.value); setMarketHistory(null); }} required /></label>
                          <label>Through<input type="date" value={marketEnd} onChange={(event) => { setMarketEnd(event.target.value); setMarketHistory(null); }} required /></label>
                      </div>
                      <button className="primary-button" type="button" onClick={loadMarketHistory} disabled={loadingMarketHistory}>{loadingMarketHistory ? 'Loading history…' : 'Load history'}</button>
                    </> : <p className="table-empty">Choose a search result to inspect instrument details and available bars.</p>}
                  </section>
                </div>
                {selectedInstrument && <section className="data-section instrument-detail-panel">
                  <div className="instrument-detail-tabs" role="tablist" aria-label="Instrument details">
                    <button type="button" role="tab" aria-selected={detailTab === 'indicators'} className={detailTab === 'indicators' ? 'active' : ''} onClick={() => setDetailTab('indicators')}>Indicators</button>
                    <button type="button" role="tab" aria-selected={detailTab === 'fundamentals'} className={detailTab === 'fundamentals' ? 'active' : ''} onClick={() => setDetailTab('fundamentals')}>Fundamentals</button>
                  </div>
                  {detailTab === 'indicators' && <>
                  <div className="section-heading">
                    <div><p className="eyebrow">STORED OHLCV ONLY</p><h2>{selectedInstrument.symbol} indicators</h2></div>
                    <span className={`freshness-badge ${marketIndicators?.freshness.is_synthetic ? 'freshness-synthetic' : `freshness-${marketIndicators?.freshness.status ?? 'unavailable'}`}`}>
                      {marketIndicators?.freshness.is_synthetic ? 'synthetic' : marketIndicators?.freshness.status ?? (loadingMarketIndicators ? 'loading' : 'unavailable')}
                    </span>
                  </div>
                  {loadingMarketIndicators ? <p className="loading-inline">Calculating from stored bars…</p> : marketIndicators ? <>
                    <p className="market-provenance">{marketIndicators.freshness.message} Source: {marketIndicators.source ?? '—'} · Latest bar: {marketIndicators.latest_bar ? new Date(marketIndicators.latest_bar).toLocaleString() : 'Unavailable'} · {marketIndicators.bars_available} stored bars.</p>
                    <div className="indicator-grid">{Object.entries(marketIndicators.indicators).map(([key, result]) => {
                      const definition = indicatorLabels[key] ?? { label: key, unit: 'index' as const };
                      const displayValue = result.value === null
                        ? `Insufficient history · ${result.bars_used}/${result.bars_required} bars`
                        : definition.unit === 'price'
                          ? formatMoney(result.value, selectedInstrument.currency)
                          : `${Number(result.value).toFixed(2)}${definition.unit === 'percent' ? '%' : ''}`;
                      return <article className="indicator-item" key={key}>
                        <span>{definition.label}</span>
                        <strong>{displayValue}</strong>
                        <small>
                          {result.is_synthetic
                            ? <span className="freshness-badge freshness-synthetic">synthetic input</span>
                            : `${result.bars_used} bars used`}
                        </small>
                      </article>;
                    })}</div>
                    <p className="valuation-footnote">{marketIndicators.disclaimer} Indicators use stored {marketIndicators.interval} OHLCV bars and are not forecasts.</p>
                  </> : <p className="table-empty">Indicator data is unavailable.</p>}
                  </>}
                  {detailTab === 'fundamentals' && <div className="fundamentals-panel">
                    <div className="section-heading">
                      <div><p className="eyebrow">PROVIDER-REPORTED COMPANY DATA</p><h2>{selectedInstrument.symbol} fundamentals</h2></div>
                      <button className="secondary-button" type="button" onClick={() => void syncFundamentals()} disabled={fundamentalsSyncing || selectedInstrument.is_synthetic}>{fundamentalsSyncing ? 'Syncing…' : 'Sync fundamentals'}</button>
                    </div>
                    <p className="accounting-note">Currency: {selectedInstrument.currency}. Values are not converted. Missing provider fields are unavailable, never shown as zero.</p>
                    {fundamentalsMessage && <p className="market-provenance" role="status">{fundamentalsMessage}</p>}
                    {fundamentalsLoading ? <p className="loading-inline">Loading stored fundamentals…</p> : fundamentals?.unavailable ? <p className="table-empty">Fundamentals unavailable from providers. No synthetic or estimated values are available.</p> : fundamentals ? <>
                      {fundamentals.snapshot && <>
                        <div className="fundamental-snapshot-grid">
                          <article><span>Market cap</span><strong>{fundamentals.snapshot.market_cap === null ? 'Unavailable' : formatMoney(fundamentals.snapshot.market_cap, selectedInstrument.currency)}</strong></article>
                          <article><span>Trailing P/E</span><strong>{displayFundamental(fundamentals.snapshot.pe)}</strong></article>
                          <article><span>Price / book</span><strong>{displayFundamental(fundamentals.snapshot.pb)}</strong></article>
                          <article><span>Dividend yield</span><strong>{fundamentals.snapshot.dividend_yield === null ? 'Unavailable' : `${(Number(fundamentals.snapshot.dividend_yield) * 100).toFixed(2)}%`}</strong></article>
                          <article><span>Sector</span><strong>{fundamentals.snapshot.sector || 'Unavailable'}</strong></article>
                          <article><span>Industry</span><strong>{fundamentals.snapshot.industry || 'Unavailable'}</strong></article>
                        </div>
                        <p className="market-provenance">
                          Source: {fundamentals.snapshot.source} · Fetched {new Date(fundamentals.snapshot.fetched_at).toLocaleString()} · <span className={`freshness-badge ${fundamentals.snapshot.stale ? 'freshness-stale' : 'freshness-fresh'}`}>{fundamentals.snapshot.stale ? 'stale' : 'fresh'}</span>
                        </p>
                      </>}
                      <div className="fundamental-period-toolbar">
                        <h3>Financial periods</h3>
                        <label>Period<select value={fundamentalPeriodType} onChange={(event) => setFundamentalPeriodType(event.target.value as 'annual' | 'quarterly')}><option value="annual">Annual</option><option value="quarterly">Quarterly</option></select></label>
                      </div>
                      {fundamentals.fundamentals.length === 0 ? <p className="table-empty">Fundamentals unavailable from providers for this period.</p> : <div className="table-scroll"><table><thead><tr><th>Period end</th><th>Revenue</th><th>YoY</th><th>Net income</th><th>YoY</th><th>Operating margin</th><th>EPS</th><th>YoY</th><th>FCF</th><th>YoY</th><th>Source / fetched</th></tr></thead><tbody>{fundamentals.fundamentals.map((row) => <tr key={row.id}>
                        <td>{row.period_end}</td>
                        <td>{row.revenue === null ? 'Unavailable' : formatMoney(row.revenue, row.currency)}</td>
                        <td>{displayFundamental(row.revenue_yoy_growth_pct, '%')}</td>
                        <td>{row.net_income === null ? 'Unavailable' : formatMoney(row.net_income, row.currency)}</td>
                        <td>{displayFundamental(row.net_income_yoy_growth_pct, '%')}</td>
                        <td>{displayFundamental(row.operating_margin, '%')}</td>
                        <td>{displayFundamental(row.eps)}</td>
                        <td>{displayFundamental(row.eps_yoy_growth_pct, '%')}</td>
                        <td>{row.free_cash_flow === null ? 'Unavailable' : formatMoney(row.free_cash_flow, row.currency)}</td>
                        <td>{displayFundamental(row.free_cash_flow_yoy_growth_pct, '%')}</td>
                        <td>{row.source}<small className="trade-subtext">{new Date(row.fetched_at).toLocaleString()} · <span className={row.stale ? 'stale-text' : ''}>{row.stale ? 'stale' : 'fresh'}</span></small></td>
                      </tr>)}</tbody></table></div>}
                    </> : <p className="table-empty">Fundamentals unavailable from providers.</p>}
                  </div>}
                </section>}
                {marketHistory && selectedInstrument && <section className="data-section market-history">
                  <div className="section-heading"><div><p className="eyebrow">PROVENANCE &amp; FRESHNESS</p><h2>{selectedInstrument.symbol} history</h2></div><span className={`freshness-badge freshness-${marketHistory.freshness.status}`}>{marketHistory.freshness.status.replace('_', ' ')}</span></div>
                  <p className="market-provenance">{marketHistory.freshness.message} Source: {marketHistory.freshness.source ?? '—'} · Last successful fetch: {marketHistory.freshness.last_successful_update ? new Date(marketHistory.freshness.last_successful_update).toLocaleString() : 'Never'} · Latest bar: {marketHistory.freshness.latest_bar_timestamp ? new Date(marketHistory.freshness.latest_bar_timestamp).toLocaleString() : 'Unavailable'}</p>
                  {marketHistory.bars.length === 0 ? <div className="table-empty">No bars are available for that date range.</div> : <>
                    <CandleChart bars={marketHistory.bars} currency={selectedInstrument.currency} interval={marketInterval} />
                    <div className="table-scroll"><table><thead><tr><th>Timestamp (UTC)</th><th>Open</th><th>High</th><th>Low</th><th>Close</th><th>Volume</th><th>Source</th></tr></thead><tbody>{marketHistory.bars.map((bar) => {
                      const verified = selectedInstrument.provider_identifiers.find((mapping) => mapping.provider === bar.source)?.is_verified === true;
                      return <tr key={`${bar.timestamp}-${bar.source}`}><td>{new Date(bar.timestamp).toISOString().replace('T', ' ').replace('.000Z', 'Z')}</td><td>{Number(bar.open).toFixed(2)}</td><td>{Number(bar.high).toFixed(2)}</td><td>{Number(bar.low).toFixed(2)}</td><td>{Number(bar.close).toFixed(2)}</td><td>{bar.volume}</td><td>{bar.is_synthetic ? 'Synthetic · ' : ''}{bar.is_synthetic || !verified ? 'Unverified · ' : 'Verified · '}{bar.source}</td></tr>;
                    })}</tbody></table></div>
                  </>}
                </section>}
              </section>
  );
}
