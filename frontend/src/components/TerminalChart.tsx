import { useEffect, useRef } from 'react';
import {
  CandlestickSeries,
  ColorType,
  createChart,
  createSeriesMarkers,
  HistogramSeries,
  LineSeries,
  type SeriesMarker,
  type Time,
} from 'lightweight-charts';

export type TerminalCandle = { timestamp: string; open: string; high: string; low: string; close: string; volume: string };
export type ExitOutcome = 'stop' | 'target' | 'signal' | 'other';
export type TerminalMarker = { time: string; kind: 'entry' | 'exit'; side: 'long' | 'short'; text: string; outcome?: ExitOutcome };

const PALETTE = { bg: '#11151a', text: '#8b96a3', grid: '#232a32', up: '#19d38a', down: '#ff5160', amber: '#f5a524', target: '#3de3ff' };
const seconds = (iso: string): Time => Math.floor(Date.parse(iso) / 1000) as Time;

/** Marker text/colour for a backtest exit: SL = stop hit, T = target hit, X = signal exit or reversal. */
export function tradeExitMarker(reason: string): { text: string; outcome: ExitOutcome } {
  const r = reason.toLowerCase();
  if (r === 'stop' || r === 'sl' || r.includes('stop')) return { text: 'SL', outcome: 'stop' };
  if (r === 'target' || r === 'tp' || r.includes('target')) return { text: 'T', outcome: 'target' };
  if (r === 'signal' || r.includes('signal') || r.includes('reversal')) return { text: 'X', outcome: 'signal' };
  if (r.includes('square') || r.includes('session') || r.includes('eod')) return { text: 'EOD', outcome: 'other' };
  if (r === 'end_of_data') return { text: 'end', outcome: 'other' };
  return { text: '', outcome: 'other' };
}

export function TradeLegend() {
  return (
    <p className="terminal-legend" aria-label="Chart marker legend">
      <span className="terminal-legend-long">▲ L long entry</span>
      <span className="terminal-legend-short">▼ S short entry</span>
      <span className="terminal-legend-stop">● SL stop-loss hit</span>
      <span className="terminal-legend-target">● T target hit</span>
      <span className="terminal-legend-signal">● X signal exit / reversal</span>
    </p>
  );
}

function formatter(timeZone: string, withTime: boolean) {
  const f = new Intl.DateTimeFormat('en-IN', {
    timeZone, hour12: false, day: '2-digit', month: 'short', ...(withTime ? { hour: '2-digit', minute: '2-digit' } : {}),
  });
  return (time: Time) => f.format(new Date(Number(time) * 1000));
}

type Props = {
  candles: TerminalCandle[];
  markers?: TerminalMarker[];
  height?: number;
  /** IANA zone used for axis and crosshair labels. */
  timeZone?: string;
  /** Daily bars show dates only. */
  daily?: boolean;
  ariaLabel?: string;
};

/** Candles + volume with optional backtest markers. Display only: every figure comes from the API. */
export function TerminalChart({ candles, markers = [], height = 420, timeZone = 'Asia/Kolkata', daily = false, ariaLabel = 'Candlestick chart' }: Props) {
  const ref = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const container = ref.current;
    if (!container) return undefined;
    const fmt = formatter(timeZone, !daily);
    const chart = createChart(container, {
      autoSize: true,
      height,
      layout: { background: { type: ColorType.Solid, color: PALETTE.bg }, textColor: PALETTE.text },
      grid: { vertLines: { color: PALETTE.grid }, horzLines: { color: PALETTE.grid } },
      rightPriceScale: { borderColor: PALETTE.grid },
      timeScale: { borderColor: PALETTE.grid, timeVisible: !daily, secondsVisible: false, tickMarkFormatter: (time: Time) => fmt(time) },
      localization: { timeFormatter: (time: Time) => fmt(time) },
      crosshair: { mode: 0 },
    });
    const series = chart.addSeries(CandlestickSeries, {
      upColor: PALETTE.up, downColor: PALETTE.down, borderUpColor: PALETTE.up, borderDownColor: PALETTE.down,
      wickUpColor: PALETTE.up, wickDownColor: PALETTE.down,
    });
    const volume = chart.addSeries(HistogramSeries, { priceScaleId: 'volume', priceFormat: { type: 'volume' }, lastValueVisible: false, priceLineVisible: false });
    chart.priceScale('volume').applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
    chart.priceScale('right').applyOptions({ scaleMargins: { top: 0.06, bottom: 0.22 } });
    series.setData(candles.map((c) => ({ time: seconds(c.timestamp), open: Number(c.open), high: Number(c.high), low: Number(c.low), close: Number(c.close) })));
    volume.setData(candles.map((c) => ({ time: seconds(c.timestamp), value: Number(c.volume), color: Number(c.close) >= Number(c.open) ? '#19d38a55' : '#ff516055' })));
    if (markers.length > 0 && candles.length > 0) {
      const times = candles.map((c) => Date.parse(c.timestamp) / 1000);
      const snap = (iso: string) => {
        const target = Date.parse(iso) / 1000;
        let found = times[0];
        for (const t of times) { if (t <= target) found = t; else break; }
        return found as Time;
      };
      const built: SeriesMarker<Time>[] = markers
        .filter((m) => Date.parse(m.time) / 1000 >= times[0])
        .map((m) => ({
          time: snap(m.time),
          position: m.kind === 'entry' ? (m.side === 'long' ? 'belowBar' : 'aboveBar') : (m.side === 'long' ? 'aboveBar' : 'belowBar'),
          shape: m.kind === 'entry' ? (m.side === 'long' ? 'arrowUp' : 'arrowDown') : 'circle',
          color: m.kind === 'entry' ? (m.side === 'long' ? PALETTE.up : PALETTE.down)
            : m.outcome === 'stop' ? PALETTE.down : m.outcome === 'target' ? PALETTE.target : PALETTE.amber,
          text: m.text,
        }));
      built.sort((a, b) => Number(a.time) - Number(b.time));
      createSeriesMarkers(series, built);
    }
    chart.timeScale().fitContent();
    return () => chart.remove();
  }, [candles, markers, height, timeZone, daily]);

  return <div ref={ref} className="terminal-chart" style={{ height }} role="img" aria-label={ariaLabel} />;
}

/** A simple line chart for an equity curve. */
export function TerminalLine({ points, height = 220, timeZone = 'Asia/Kolkata' }: { points: { time: string; value: string }[]; height?: number; timeZone?: string }) {
  const ref = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    const container = ref.current;
    if (!container) return undefined;
    const fmt = formatter(timeZone, true);
    const chart = createChart(container, {
      autoSize: true, height,
      layout: { background: { type: ColorType.Solid, color: PALETTE.bg }, textColor: PALETTE.text },
      grid: { vertLines: { color: PALETTE.grid }, horzLines: { color: PALETTE.grid } },
      rightPriceScale: { borderColor: PALETTE.grid },
      timeScale: { borderColor: PALETTE.grid, timeVisible: true, tickMarkFormatter: (time: Time) => fmt(time) },
      localization: { timeFormatter: (time: Time) => fmt(time) },
    });
    chart.addSeries(LineSeries, { color: PALETTE.amber, lineWidth: 2, priceLineVisible: false })
      .setData(points.map((p) => ({ time: seconds(p.time), value: Number(p.value) })));
    chart.timeScale().fitContent();
    return () => chart.remove();
  }, [points, height, timeZone]);
  return <div ref={ref} className="terminal-chart" style={{ height }} role="img" aria-label="Equity curve" />;
}
