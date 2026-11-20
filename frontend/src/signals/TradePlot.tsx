import { useEffect, useMemo, useState } from 'react';
import { apiRequest } from '../api';
import { TerminalChart, TradeLegend, tradeExitMarker, type TerminalCandle, type TerminalMarker } from '../components/TerminalChart';
import type { Trade } from './types';

const INTERVALS = ['1m', '5m', '15m'] as const;
const IST = 'Asia/Kolkata';

/** IST calendar date (YYYY-MM-DD) of an instant. */
const istDay = (iso: string) => new Intl.DateTimeFormat('en-CA', { timeZone: IST, year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date(iso));
const istTime = (iso: string) => new Intl.DateTimeFormat('en-IN', { timeZone: IST, hour: '2-digit', minute: '2-digit', hour12: false }).format(new Date(iso));
/** [start, end) of an IST day as UTC instants (IST is a fixed UTC+05:30, no DST). */
const dayWindow = (day: string) => {
  const start = new Date(`${day}T00:00:00+05:30`);
  return { start: start.toISOString(), end: new Date(start.getTime() + 86_400_000).toISOString() };
};
const money = (value: string) => Number(value).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const tone = (value: string) => (Number(value) >= 0 ? 'sl-pos' : 'sl-neg');

type History = { bars: TerminalCandle[] };

/** Candle chart of one instrument and one trading day with the backtest's entries and exits plotted. */
export function TradePlot({ trades }: { trades: Trade[] }) {
  const instruments = useMemo(() => {
    const seen = new Map<string, { id: string; symbol: string; count: number }>();
    for (const t of trades) {
      const item = seen.get(t.instrument_id) ?? { id: t.instrument_id, symbol: t.symbol, count: 0 };
      item.count += 1;
      seen.set(t.instrument_id, item);
    }
    return [...seen.values()].sort((a, b) => b.count - a.count);
  }, [trades]);

  const [instrumentId, setInstrumentId] = useState(instruments[0]?.id ?? '');
  const [interval, setIntervalValue] = useState<(typeof INTERVALS)[number]>('5m');
  const days = useMemo(
    () => [...new Set(trades.filter((t) => t.instrument_id === instrumentId).map((t) => istDay(t.entry_time)))].sort(),
    [trades, instrumentId],
  );
  const [day, setDay] = useState<string>('');
  const activeDay = days.includes(day) ? day : (days[0] ?? '');
  const [bars, setBars] = useState<TerminalCandle[]>([]);
  const [note, setNote] = useState<string | null>(null);

  const dayTrades = useMemo(
    () => trades.filter((t) => t.instrument_id === instrumentId && istDay(t.entry_time) === activeDay),
    [trades, instrumentId, activeDay],
  );
  const markers = useMemo<TerminalMarker[]>(() => dayTrades.flatMap((t) => [
    { time: t.entry_time, kind: 'entry' as const, side: t.side, text: t.side === 'long' ? 'L' : 'S' },
    { time: t.exit_time, kind: 'exit' as const, side: t.side, ...tradeExitMarker(t.exit_reason) },
  ]), [dayTrades]);

  useEffect(() => {
    if (!instrumentId || !activeDay) { setBars([]); return undefined; }
    let cancelled = false;
    setNote(null);
    const { start, end } = dayWindow(activeDay);
    const params = new URLSearchParams({ interval, start, end, limit: '500' });
    apiRequest<History>(`/market-data/instruments/${instrumentId}/history?${params}`)
      .then((h) => { if (!cancelled) { setBars(h.bars); setNote(h.bars.length ? null : 'No stored bars for this day.'); } })
      .catch((e: unknown) => { if (!cancelled) { setBars([]); setNote(e instanceof Error ? e.message : 'Bars could not be loaded.'); } });
    return () => { cancelled = true; };
  }, [instrumentId, activeDay, interval]);

  async function loadFromBroker() {
    const { start, end } = dayWindow(activeDay);
    const params = new URLSearchParams({ interval, start, end, limit: '500' });
    setNote('Loading bars from the broker…');
    try {
      const h = await apiRequest<History>(`/market-data/instruments/${instrumentId}/history/sync?${params}`, { method: 'POST' });
      setBars(h.bars);
      setNote(h.bars.length ? null : 'The broker returned no bars for this day.');
    } catch (e) { setNote(e instanceof Error ? e.message : 'Bars could not be loaded.'); }
  }

  if (trades.length === 0) return <div className="sl-card"><p className="sl-muted">This run has no trades to plot.</p></div>;
  const index = days.indexOf(activeDay);
  const symbol = instruments.find((i) => i.id === instrumentId)?.symbol ?? '';

  return (
    <div className="sl-card trade-plot">
      <h4>Trade chart</h4>
      <div className="sl-row trade-plot-controls">
        <label>Instrument<select value={instrumentId} onChange={(e) => { setInstrumentId(e.target.value); setDay(''); }}>
          {instruments.map((i) => <option key={i.id} value={i.id}>{i.symbol} ({i.count} trades)</option>)}</select></label>
        <label>Trading day<select value={activeDay} onChange={(e) => setDay(e.target.value)}>
          {days.map((d) => <option key={d} value={d}>{d}</option>)}</select></label>
        <div className="trade-plot-nav" role="group" aria-label="Day navigation">
          <button type="button" disabled={index <= 0} onClick={() => setDay(days[index - 1])}>‹ Previous day</button>
          <button type="button" disabled={index >= days.length - 1} onClick={() => setDay(days[index + 1])}>Next day ›</button>
        </div>
        <div className="crypto-resolutions" role="group" aria-label="Candle size">
          {INTERVALS.map((i) => <button key={i} type="button" className={i === interval ? 'active' : ''} aria-pressed={i === interval} onClick={() => setIntervalValue(i)}>{i}</button>)}
        </div>
      </div>
      {bars.length > 0
        ? <TerminalChart candles={bars} markers={markers} height={400} ariaLabel={`${symbol} candles with backtest trades`} />
        : <div className="sl-muted trade-plot-empty">{note ?? 'Loading…'}{note && note.startsWith('No stored') && <> <button type="button" className="sl-link" onClick={() => void loadFromBroker()}>Load from broker</button></>}</div>}
      <TradeLegend />
      <p className="sl-muted">Markers are placed at the bar where each fill happened (IST). For options strategies the chart shows the underlying; fills are on the option contract.</p>
      <table className="sl-table sl-compact" aria-label="Trades on this day">
        <thead><tr><th>Entry</th><th>Side</th><th className="sl-r">Entry fill</th><th>Exit</th><th className="sl-r">Exit fill</th><th>Reason</th><th className="sl-r">Net P&amp;L</th></tr></thead>
        <tbody>{dayTrades.map((t) => (
          <tr key={`${t.entry_time}-${t.exit_time}-${t.side}`}>
            <td>{istTime(t.entry_time)}</td><td>{t.side}</td><td className="sl-r">{money(t.entry_fill)}</td>
            <td>{istTime(t.exit_time)}</td><td className="sl-r">{money(t.exit_fill)}</td><td>{t.exit_reason.replace(/_/g, ' ')}</td>
            <td className={`sl-r ${tone(t.net_pnl)}`}>{money(t.net_pnl)}</td>
          </tr>
        ))}</tbody>
      </table>
    </div>
  );
}
