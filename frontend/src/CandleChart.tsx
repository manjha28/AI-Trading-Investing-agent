import { useEffect, useRef, useState } from 'react';
import {
  CandlestickSeries,
  ColorType,
  createChart,
  HistogramSeries,
  LineSeries,
  type MouseEventParams,
  type Time,
} from 'lightweight-charts';

type Candle = {
  timestamp: string;
  open: string;
  high: string;
  low: string;
  close: string;
  volume: string;
};

type Props = {
  bars: Candle[];
  currency: string;
  interval: string;
};

type Readout = { time: string; open: number; high: number; low: number; close: number } | null;

function price(value: number, currency: string) {
  if (currency.length !== 3) {
    return `${new Intl.NumberFormat('en-US', {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    }).format(value)} ${currency}`;
  }
  return new Intl.NumberFormat('en-US', {
    style: 'currency',
    currency,
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(value);
}

export function CandleChart({ bars, currency, interval }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [chartMode, setChartMode] = useState<'candles' | 'line'>('candles');
  const [readout, setReadout] = useState<Readout>(null);

  useEffect(() => {
    const container = containerRef.current;
    if (!container || bars.length === 0) return;

    const chart = createChart(container, {
      autoSize: true,
      height: 340,
      layout: { background: { type: ColorType.Solid, color: '#fbfcfa' }, textColor: '#5d6c62' },
      grid: { vertLines: { color: '#edf0eb' }, horzLines: { color: '#edf0eb' } },
      rightPriceScale: { scaleMargins: { top: 0.08, bottom: 0.25 } },
      timeScale: { timeVisible: interval !== '1d', secondsVisible: false, borderColor: '#dfe5dc' },
      crosshair: { mode: 0 },
      localization: { priceFormatter: (value: number) => price(value, currency) },
    });
    const candleSeries = chart.addSeries(CandlestickSeries, {
      upColor: '#17674d',
      downColor: '#bd5547',
      borderUpColor: '#17674d',
      borderDownColor: '#bd5547',
      wickUpColor: '#17674d',
      wickDownColor: '#bd5547',
      visible: chartMode === 'candles',
    });
    const lineSeries = chart.addSeries(LineSeries, {
      color: '#17674d',
      lineWidth: 2,
      visible: chartMode === 'line',
    });
    const volumeSeries = chart.addSeries(HistogramSeries, {
      priceScaleId: 'volume',
      priceFormat: { type: 'volume' },
      lastValueVisible: false,
      priceLineVisible: false,
    });
    chart.priceScale('volume').applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });

    const sorted = [...bars].sort(
      (left, right) => new Date(left.timestamp).getTime() - new Date(right.timestamp).getTime(),
    );
    const data = sorted.map((bar) => ({
      time: Math.floor(new Date(bar.timestamp).getTime() / 1000) as Time,
      open: Number(bar.open),
      high: Number(bar.high),
      low: Number(bar.low),
      close: Number(bar.close),
    }));
    candleSeries.setData(data);
    lineSeries.setData(data.map(({ time, close }) => ({ time, value: close })));
    volumeSeries.setData(
      sorted.map((bar, index) => ({
        time: data[index].time,
        value: Number(bar.volume),
        color: Number(bar.close) >= Number(bar.open) ? '#17674d66' : '#bd554766',
      })),
    );
    chart.timeScale().fitContent();
    const onCrosshairMove = (event: MouseEventParams<Time>) => {
      const candle = event.seriesData.get(candleSeries);
      const time = event.time;
      const source = candle && 'open' in candle
        ? candle
        : typeof time === 'number'
          ? data.find((item) => item.time === time)
          : undefined;
      if (!source || typeof time === 'undefined') {
        setReadout(null);
        return;
      }
      setReadout({
        time: typeof time === 'number' ? new Date(time * 1000).toLocaleString() : String(time),
        open: source.open,
        high: source.high,
        low: source.low,
        close: source.close,
      });
    };
    chart.subscribeCrosshairMove(onCrosshairMove);
    return () => {
      chart.unsubscribeCrosshairMove(onCrosshairMove);
      chart.remove();
    };
  }, [bars, chartMode, currency, interval]);

  return (
    <div className="candlestick-panel">
      <div className="candlestick-toolbar">
        <div className="candlestick-toggle" role="group" aria-label="Chart style">
          <button type="button" className={chartMode === 'candles' ? 'selected' : ''} onClick={() => setChartMode('candles')}>Candles</button>
          <button type="button" className={chartMode === 'line' ? 'selected' : ''} onClick={() => setChartMode('line')}>Line</button>
        </div>
        <div className="crosshair-readout" aria-live="polite">
          {readout
            ? `${readout.time} · O ${price(readout.open, currency)} H ${price(readout.high, currency)} L ${price(readout.low, currency)} C ${price(readout.close, currency)}`
            : 'Move crosshair over chart for OHLC'}
        </div>
      </div>
      <div className="ohlc-chart" ref={containerRef} aria-label={`${interval} candlestick chart with volume`} />
    </div>
  );
}
