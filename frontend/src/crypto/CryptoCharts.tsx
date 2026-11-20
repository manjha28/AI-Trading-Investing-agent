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

export type ChartCandle = { timestamp: string; open: string; high: string; low: string; close: string; volume: string };
export type ExitOutcome = 'stop' | 'target' | 'signal' | 'other';
export type TradeMarker = { time: string; kind: 'entry' | 'exit'; side: 'long' | 'short'; text: string; outcome?: ExitOutcome };

/** Marker text and colour for an exit, so a stop (SL), a hit target (T) and a signal reversal (X) are distinguishable. */
export function exitMarker(reason: string): { text: string; outcome: ExitOutcome } {
  if (reason === 'stop') return { text: 'SL', outcome: 'stop' };
  if (reason === 'target') return { text: 'T', outcome: 'target' };
  if (reason === 'signal') return { text: 'X', outcome: 'signal' };
  return { text: reason === 'data_gap' ? 'gap' : reason === 'end_of_data' ? 'end' : '', outcome: 'other' };
}

export function MarkerLegend() {
  return (
    <p className="crypto-legend" aria-label="Chart marker legend">
      <span className="crypto-legend-long">▲ L long entry</span>
      <span className="crypto-legend-short">▼ S short entry</span>
      <span className="crypto-legend-stop">● SL stop-loss hit</span>
      <span className="crypto-legend-target">● T target hit</span>
      <span className="crypto-legend-signal">● X signal exit / reversal</span>
    </p>
  );
}

const PALETTE = { bg: '#11151a', text: '#8b96a3', grid: '#232a32', up: '#19d38a', down: '#ff5160', amber: '#f5a524', target: '#3de3ff' };

const seconds = (iso: string): Time => Math.floor(Date.parse(iso) / 1000) as Time;

function baseOptions(height: number) {
  return {
    autoSize: true,
    height,
    layout: { background: { type: ColorType.Solid, color: PALETTE.bg }, textColor: PALETTE.text },
    grid: { vertLines: { color: PALETTE.grid }, horzLines: { color: PALETTE.grid } },
    rightPriceScale: { borderColor: PALETTE.grid },
    timeScale: { borderColor: PALETTE.grid, timeVisible: true, secondsVisible: false },
    crosshair: { mode: 0 as const },
  };
}

/** Candles (completed only) with volume and optional backtest entry/exit markers. Display only: all figures come from the API. */
export function CryptoCandleChart({ candles, markers = [], height = 420 }: { candles: ChartCandle[]; markers?: TradeMarker[]; height?: number }) {
  const ref = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const container = ref.current;
    if (!container) return undefined;
    const chart = createChart(container, baseOptions(height));
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
  }, [candles, markers, height]);

  return <div ref={ref} className="crypto-chart" style={{ height }} role="img" aria-label="Candlestick chart of completed candles" />;
}

/** Equity curve of a hypothetical backtest. */
export function EquityChart({ points, height = 220 }: { points: { time: string; equity: string }[]; height?: number }) {
  const ref = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    const container = ref.current;
    if (!container) return undefined;
    const chart = createChart(container, baseOptions(height));
    const line = chart.addSeries(LineSeries, { color: PALETTE.amber, lineWidth: 2, priceLineVisible: false });
    line.setData(points.map((p) => ({ time: seconds(p.time), value: Number(p.equity) })));
    chart.timeScale().fitContent();
    return () => chart.remove();
  }, [points, height]);
  return <div ref={ref} className="crypto-chart" style={{ height }} role="img" aria-label="Hypothetical equity curve" />;
}
