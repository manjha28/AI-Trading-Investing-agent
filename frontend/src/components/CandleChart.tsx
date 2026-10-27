import { useEffect, useRef, useState } from 'react';
import {
  CandlestickSeries,
  ColorType,
  createChart,
  HistogramSeries,
  LineSeries,
  type IChartApi,
  type ISeriesApi,
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
  source?: string;
};

type IndicatorPoint = { timestamp: string; value: string };
type IndicatorSeries = Record<string, IndicatorPoint[]>;

type Props = {
  bars: Candle[];
  liveBar?: Candle | null;
  tickSize?: string | null;
  currency: string;
  timezone: string;
  interval: string;
  preferredSource?: string | null;
  indicatorSeries?: IndicatorSeries;
  indicatorBarsAvailable?: number;
  onLoadOlder?: () => void;
  loadingOlder?: boolean;
  hasOlder?: boolean;
};

type Readout = { time: string; open: number; high: number; low: number; close: number } | null;

const INDICATORS = [
  { key: 'sma_20', label: 'SMA 20', color: '#f4b860', pane: 0, kind: 'line' },
  { key: 'sma_50', label: 'SMA 50', color: '#b4a0ff', pane: 0, kind: 'line' },
  { key: 'sma_200', label: 'SMA 200', color: '#e783ce', pane: 0, kind: 'line' },
  { key: 'ema_20', label: 'EMA 20', color: '#49c5d4', pane: 0, kind: 'line' },
  { key: 'ema_50', label: 'EMA 50', color: '#70a6ff', pane: 0, kind: 'line' },
  { key: 'ema_200', label: 'EMA 200', color: '#fa8f6f', pane: 0, kind: 'line' },
  { key: 'vwap', label: 'VWAP', color: '#fff176', pane: 0, kind: 'line' },
  { key: 'bb_upper', label: 'Bollinger upper', color: '#7a9eaa', pane: 0, kind: 'line' },
  { key: 'bb_middle', label: 'Bollinger middle', color: '#8296a4', pane: 0, kind: 'line' },
  { key: 'bb_lower', label: 'Bollinger lower', color: '#7a9eaa', pane: 0, kind: 'line' },
  { key: 'rsi_14', label: 'RSI 14', color: '#91a6ff', pane: 1, kind: 'line' },
  { key: 'macd', label: 'MACD', color: '#70d6c1', pane: 2, kind: 'line' },
  { key: 'macd_signal', label: 'MACD signal', color: '#f4b860', pane: 2, kind: 'line' },
  { key: 'atr_14', label: 'ATR 14', color: '#f494b4', pane: 3, kind: 'line' },
] as const;

const INITIAL_INDICATORS = ['sma_20', 'volume', 'rsi_14'];
const INDICATOR_WARMUP_BARS: Record<string, number> = {
  sma_20: 19,
  sma_50: 49,
  sma_200: 199,
  ema_20: 19,
  ema_50: 49,
  ema_200: 199,
  vwap: 0,
  bb_upper: 19,
  bb_middle: 19,
  bb_lower: 19,
  rsi_14: 14,
  macd: 25,
  macd_signal: 33,
  macd_histogram: 33,
  atr_14: 14,
};

function quantizePrice(value: string, tickSize: string | null | undefined): string {
  if (!tickSize || !/^-?\d+(?:\.\d+)?$/.test(value) || !/^\d+(?:\.\d+)?$/.test(tickSize)) {
    return value;
  }
  const scale = Math.max(
    value.split('.')[1]?.length ?? 0,
    tickSize.split('.')[1]?.length ?? 0,
  );
  const units = (raw: string) => {
    const [whole, fraction = ''] = raw.split('.');
    return BigInt(`${whole}${fraction.padEnd(scale, '0')}`);
  };
  const priceUnits = units(value);
  const tickUnits = units(tickSize);
  if (tickUnits <= 0n) return value;
  const quotient = priceUnits / tickUnits;
  const remainder = priceUnits % tickUnits;
  const rounded = (
    remainder * 2n >= tickUnits ? quotient + 1n : quotient
  ) * tickUnits;
  const sign = rounded < 0n ? '-' : '';
  const digits = (rounded < 0n ? -rounded : rounded).toString().padStart(scale + 1, '0');
  return scale
    ? `${sign}${digits.slice(0, -scale)}.${digits.slice(-scale)}`
    : `${sign}${digits}`;
}

function compareDecimals(left: string, right: string): number {
  const scale = Math.max(left.split('.')[1]?.length ?? 0, right.split('.')[1]?.length ?? 0);
  const units = (raw: string) => {
    const [whole, fraction = ''] = raw.split('.');
    return BigInt(`${whole}${fraction.padEnd(scale, '0')}`);
  };
  const leftUnits = units(left);
  const rightUnits = units(right);
  return leftUnits === rightUnits ? 0 : leftUnits > rightUnits ? 1 : -1;
}

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

function chartDate(time: Time): Date {
  if (typeof time === 'number') return new Date(time * 1000);
  if (typeof time === 'string') return new Date(`${time}T00:00:00.000Z`);
  return new Date(Date.UTC(time.year, time.month - 1, time.day));
}

function formatChartTime(time: Time, timezone: string, includeTime = true): string {
  return new Intl.DateTimeFormat(undefined, {
    timeZone: timezone,
    year: 'numeric',
    month: 'short',
    day: '2-digit',
    ...(includeTime ? { hour: '2-digit', minute: '2-digit' } : {}),
  }).format(chartDate(time));
}

export function CandleChart({
  bars,
  liveBar = null,
  tickSize = null,
  currency,
  timezone,
  interval,
  preferredSource,
  indicatorSeries = {},
  indicatorBarsAvailable,
  onLoadOlder,
  loadingOlder = false,
  hasOlder = false,
}: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleSeriesRef = useRef<ISeriesApi<'Candlestick'> | null>(null);
  const lineSeriesRef = useRef<ISeriesApi<'Line'> | null>(null);
  const volumeSeriesRef = useRef<ISeriesApi<'Histogram'> | null>(null);
  const macdHistogramRef = useRef<ISeriesApi<'Histogram'> | null>(null);
  const indicatorSeriesRef = useRef<Map<string, ISeriesApi<'Line'>>>(new Map());
  const olderCallbackRef = useRef(onLoadOlder);
  const loadingOlderRef = useRef(loadingOlder);
  const hasOlderRef = useRef(hasOlder);
  const liveBarRef = useRef<Candle | null>(null);
  const liveTimerRef = useRef<number | null>(null);
  const liveConfigRef = useRef({ bars, interval, tickSize });
  const liveCandleRef = useRef<{
    time: number;
    open: string;
    high: string;
    low: string;
  } | null>(null);
  const userNavigatedRef = useRef(false);
  const [chartMode, setChartMode] = useState<'candles' | 'line'>('candles');
  const [enabledIndicators, setEnabledIndicators] = useState<string[]>(INITIAL_INDICATORS);
  const [indicatorsOpen, setIndicatorsOpen] = useState(false);
  const [readout, setReadout] = useState<Readout>(null);

  olderCallbackRef.current = onLoadOlder;
  loadingOlderRef.current = loadingOlder;
  hasOlderRef.current = hasOlder;
  liveConfigRef.current = { bars, interval, tickSize };

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    const chart = createChart(container, {
      autoSize: true,
      height: 430,
      layout: { background: { type: ColorType.Solid, color: '#0b0f16' }, textColor: '#94a1ad' },
      grid: { vertLines: { color: '#171e29' }, horzLines: { color: '#171e29' } },
      rightPriceScale: { scaleMargins: { top: 0.08, bottom: 0.25 } },
      timeScale: {
        timeVisible: interval !== '1d',
        secondsVisible: false,
        borderColor: '#222b36',
        tickMarkFormatter: (time: Time) => formatChartTime(time, timezone, interval !== '1d'),
      },
      crosshair: { mode: 0 },
      localization: {
        priceFormatter: (value: number) => price(value, currency),
        timeFormatter: (time: Time) => formatChartTime(time, timezone),
      },
    });
    const candleSeries = chart.addSeries(CandlestickSeries, {
      upColor: '#3fd99a',
      downColor: '#ef7777',
      borderUpColor: '#3fd99a',
      borderDownColor: '#ef7777',
      wickUpColor: '#3fd99a',
      wickDownColor: '#ef7777',
    });
    const lineSeries = chart.addSeries(LineSeries, { color: '#3fd99a', lineWidth: 2 });
    const volumeSeries = chart.addSeries(HistogramSeries, {
      priceScaleId: 'volume',
      priceFormat: { type: 'volume' },
      lastValueVisible: false,
      priceLineVisible: false,
    });
    const indicatorSeries = new Map<string, ISeriesApi<'Line'>>();
    INDICATORS.forEach((indicator) => {
      if (indicator.pane !== 0) return;
      indicatorSeries.set(
        indicator.key,
        chart.addSeries(
          LineSeries,
          {
            color: indicator.color,
            lineWidth: indicator.key.startsWith('bb_') ? 1 : 2,
            priceLineVisible: false,
            lastValueVisible: false,
          },
          0,
        ),
      );
    });
    chart.priceScale('volume').applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
    chartRef.current = chart;
    candleSeriesRef.current = candleSeries;
    lineSeriesRef.current = lineSeries;
    volumeSeriesRef.current = volumeSeries;
    macdHistogramRef.current = null;
    indicatorSeriesRef.current = indicatorSeries;

    const onCrosshairMove = (event: MouseEventParams<Time>) => {
      const candle = event.seriesData.get(candleSeries);
      if (!candle || !('open' in candle) || typeof event.time === 'undefined') {
        setReadout(null);
        return;
      }
      setReadout({
        time: formatChartTime(event.time, timezone),
        open: candle.open,
        high: candle.high,
        low: candle.low,
        close: candle.close,
      });
    };
    const onVisibleRangeChange = () => {
      const visibleRange = chart.timeScale().getVisibleLogicalRange();
      if (
        userNavigatedRef.current
        && visibleRange
        && visibleRange.from <= 3
        && hasOlderRef.current
        && !loadingOlderRef.current
      ) olderCallbackRef.current?.();
    };
    const markUserNavigation = () => {
      userNavigatedRef.current = true;
    };
    chart.subscribeCrosshairMove(onCrosshairMove);
    chart.timeScale().subscribeVisibleLogicalRangeChange(onVisibleRangeChange);
    container.addEventListener('wheel', markUserNavigation, { passive: true });
    container.addEventListener('mousedown', markUserNavigation);
    return () => {
      chart.unsubscribeCrosshairMove(onCrosshairMove);
      chart.timeScale().unsubscribeVisibleLogicalRangeChange(onVisibleRangeChange);
      container.removeEventListener('wheel', markUserNavigation);
      container.removeEventListener('mousedown', markUserNavigation);
      chart.remove();
      chartRef.current = null;
      candleSeriesRef.current = null;
      lineSeriesRef.current = null;
      volumeSeriesRef.current = null;
      macdHistogramRef.current = null;
      indicatorSeriesRef.current.clear();
    };
  }, [currency, interval, timezone]);

  useEffect(() => {
    liveCandleRef.current = null;
  }, [bars, interval]);

  const inferredSeriesBars = Math.max(
    0,
    ...Object.entries(indicatorSeries).map(([key, points]) => (
      points.length + (INDICATOR_WARMUP_BARS[key] ?? 0)
    )),
  );
  const fullSeriesBarsAvailable = Math.max(
    indicatorBarsAvailable ?? 0,
    inferredSeriesBars,
    bars.length,
  );
  const availableIndicators = [
    { key: 'sma_20', label: 'SMA 20', period: 20, available: Boolean(indicatorSeries.sma_20?.length) },
    { key: 'sma_50', label: 'SMA 50', period: 50, available: Boolean(indicatorSeries.sma_50?.length) },
    { key: 'sma_200', label: 'SMA 200', period: 200, available: Boolean(indicatorSeries.sma_200?.length) },
    { key: 'ema_20', label: 'EMA 20', period: 20, available: Boolean(indicatorSeries.ema_20?.length) },
    { key: 'ema_50', label: 'EMA 50', period: 50, available: Boolean(indicatorSeries.ema_50?.length) },
    { key: 'ema_200', label: 'EMA 200', period: 200, available: Boolean(indicatorSeries.ema_200?.length) },
    { key: 'vwap', label: 'VWAP', period: 1, available: Boolean(indicatorSeries.vwap?.length) },
    {
      key: 'bb_upper',
      label: 'Bollinger Bands',
      period: 20,
      available: Boolean(indicatorSeries.bb_upper?.length),
    },
    { key: 'rsi_14', label: 'RSI 14', period: 15, available: Boolean(indicatorSeries.rsi_14?.length) },
    {
      key: 'macd',
      label: 'MACD',
      period: 34,
      available: Boolean(
        indicatorSeries.macd?.length
        && indicatorSeries.macd_signal?.length
        && indicatorSeries.macd_histogram?.length,
      ),
    },
    { key: 'atr_14', label: 'ATR 14', period: 15, available: Boolean(indicatorSeries.atr_14?.length) },
    { key: 'volume', label: 'Volume', period: 1, available: bars.length > 0 },
  ].map((indicator) => ({
    ...indicator,
    available: fullSeriesBarsAvailable >= indicator.period,
  }));
  const activeIndicatorCount = availableIndicators.filter(
    (indicator) => indicator.available && enabledIndicators.includes(indicator.key),
  ).length;

  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    const series = indicatorSeriesRef.current;
    const activeOscillators = [
      { key: 'rsi_14', enabled: enabledIndicators.includes('rsi_14') && Boolean(indicatorSeries.rsi_14?.length) },
      { key: 'macd', enabled: enabledIndicators.includes('macd') && Boolean(indicatorSeries.macd?.length && indicatorSeries.macd_signal?.length && indicatorSeries.macd_histogram?.length) },
      { key: 'atr_14', enabled: enabledIndicators.includes('atr_14') && Boolean(indicatorSeries.atr_14?.length) },
    ].filter((item) => item.enabled);
    for (const key of ['rsi_14', 'macd', 'macd_signal', 'atr_14']) {
      const existing = series.get(key);
      if (existing) {
        chart.removeSeries(existing);
        series.delete(key);
      }
    }
    if (macdHistogramRef.current) {
      chart.removeSeries(macdHistogramRef.current);
      macdHistogramRef.current = null;
    }
    activeOscillators.forEach(({ key }, index) => {
      const paneIndex = index + 1;
      const addLine = (seriesKey: string) => {
        if (series.has(seriesKey)) return;
        const indicator = INDICATORS.find((item) => item.key === seriesKey);
        if (!indicator) return;
        const line = chart.addSeries(LineSeries, {
          color: indicator.color,
          lineWidth: 2,
          priceLineVisible: false,
          lastValueVisible: false,
        }, paneIndex);
        line.setData((indicatorSeries[seriesKey] ?? []).map((point) => ({
          time: Math.floor(new Date(point.timestamp).getTime() / 1000) as Time,
          value: Number(point.value),
        })));
        series.set(seriesKey, line);
      };
      if (key === 'macd') {
        addLine('macd');
        addLine('macd_signal');
        if (!macdHistogramRef.current) {
          const histogram = chart.addSeries(
            HistogramSeries,
            { priceLineVisible: false, lastValueVisible: false },
            paneIndex,
          );
          histogram.setData((indicatorSeries.macd_histogram ?? []).map((point) => ({
            time: Math.floor(new Date(point.timestamp).getTime() / 1000) as Time,
            value: Number(point.value),
            color: Number(point.value) >= 0 ? '#3fd99a99' : '#ef777799',
          })));
          macdHistogramRef.current = histogram;
        }
      } else {
        addLine(key);
      }
    });
    chart.panes().forEach((pane, index) => pane.setStretchFactor(index === 0 ? 3 : 1));
  }, [enabledIndicators, indicatorSeries]);

  useEffect(() => {
    const candleSeries = candleSeriesRef.current;
    const lineSeries = lineSeriesRef.current;
    const volumeSeries = volumeSeriesRef.current;
    const chart = chartRef.current;
    if (!candleSeries || !lineSeries || !volumeSeries || !chart || bars.length === 0) return;

    const sorted = [...bars].sort(
      (left, right) => new Date(left.timestamp).getTime() - new Date(right.timestamp).getTime(),
    );
    const barsByTimestamp = new Map<string, Candle>();
    sorted.forEach((bar) => {
      const previous = barsByTimestamp.get(bar.timestamp);
      if (
        !previous
        || (bar.source === preferredSource && previous.source !== preferredSource)
        || (!preferredSource && bar.source === 'dhan' && previous.source !== 'dhan')
      ) barsByTimestamp.set(bar.timestamp, bar);
    });
    const uniqueBars = [...barsByTimestamp.values()];
    const data = uniqueBars.map((bar) => ({
      time: Math.floor(new Date(bar.timestamp).getTime() / 1000) as Time,
      open: Number(bar.open),
      high: Number(bar.high),
      low: Number(bar.low),
      close: Number(bar.close),
    }));
    const visibleRange = chart.timeScale().getVisibleLogicalRange();
    candleSeries.setData(data);
    lineSeries.setData(data.map(({ time, close }) => ({ time, value: close })));
    volumeSeries.setData(
      uniqueBars.map((bar, index) => ({
        time: data[index].time,
        value: Number(bar.volume),
        color: Number(bar.close) >= Number(bar.open) ? '#3fd99a66' : '#ef777766',
      })),
    );
    indicatorSeriesRef.current.forEach((series, key) => {
      series.setData((indicatorSeries[key] ?? []).map((point) => ({
        time: Math.floor(new Date(point.timestamp).getTime() / 1000) as Time,
        value: Number(point.value),
      })));
    });
    macdHistogramRef.current?.setData((indicatorSeries.macd_histogram ?? []).map((point) => ({
      time: Math.floor(new Date(point.timestamp).getTime() / 1000) as Time,
      value: Number(point.value),
      color: Number(point.value) >= 0 ? '#3fd99a99' : '#ef777799',
    })));
    if (visibleRange) {
      chart.timeScale().setVisibleLogicalRange(visibleRange);
    } else {
      chart.timeScale().setVisibleRange({
        from: data[0].time,
        to: data[data.length - 1].time,
      });
    }
  }, [bars, indicatorSeries, preferredSource]);

  useEffect(() => {
    if (!liveBar) return;
    liveBarRef.current = liveBar;
    if (liveTimerRef.current !== null) return;
    liveTimerRef.current = window.setTimeout(() => {
      liveTimerRef.current = null;
      const tick = liveBarRef.current;
      const config = liveConfigRef.current;
      const candleSeries = candleSeriesRef.current;
      if (!tick || !candleSeries || !config.tickSize || config.bars.length === 0) return;
      const latestBar = config.bars.reduce((latest, bar) => (
        new Date(bar.timestamp).getTime() > new Date(latest.timestamp).getTime()
          ? bar
          : latest
      ));
      const tickTimestamp = new Date(tick.timestamp).getTime();
      if (!Number.isFinite(tickTimestamp)) return;
      const intervalMs: Record<string, number> = {
        '1m': 60_000,
        '5m': 300_000,
        '15m': 900_000,
        '1h': 3_600_000,
      };
      let bucketTimestamp = tickTimestamp;
      if (config.interval === '1d') {
        const istOffset = 330 * 60_000;
        const tickDay = new Date(tickTimestamp + istOffset).toISOString().slice(0, 10);
        const latestDay = new Date(
          new Date(latestBar.timestamp).getTime() + istOffset,
        ).toISOString().slice(0, 10);
        if (tickDay === latestDay) bucketTimestamp = new Date(latestBar.timestamp).getTime();
        else bucketTimestamp = new Date(`${tickDay}T00:00:00.000Z`).getTime();
      } else {
        const duration = intervalMs[config.interval];
        if (!duration) return;
        const istOffset = 330 * 60_000;
        bucketTimestamp = Math.floor((tickTimestamp + istOffset) / duration) * duration - istOffset;
      }
      const latestTimestamp = new Date(latestBar.timestamp).getTime();
      if (bucketTimestamp < latestTimestamp) return;
      const previousLiveCandle = liveCandleRef.current;
      const sameBucket = bucketTimestamp === latestTimestamp
        || previousLiveCandle?.time === bucketTimestamp;
      const closeText = quantizePrice(tick.close, config.tickSize);
      const previousHigh = sameBucket && previousLiveCandle?.time === bucketTimestamp
        ? previousLiveCandle.high
        : latestBar.high;
      const previousLow = sameBucket && previousLiveCandle?.time === bucketTimestamp
        ? previousLiveCandle.low
        : latestBar.low;
      const previousOpen = sameBucket && previousLiveCandle?.time === bucketTimestamp
        ? previousLiveCandle.open
        : latestBar.open;
      const tickHigh = config.interval === '1d' ? tick.high : tick.close;
      const tickLow = config.interval === '1d' ? tick.low : tick.close;
      const highCandidate = sameBucket && compareDecimals(previousHigh, tickHigh) > 0
        ? previousHigh
        : tickHigh;
      const lowCandidate = sameBucket && compareDecimals(previousLow, tickLow) < 0
        ? previousLow
        : tickLow;
      const highText = quantizePrice(
        compareDecimals(highCandidate, closeText) < 0 ? closeText : highCandidate,
        config.tickSize,
      );
      const lowText = quantizePrice(
        compareDecimals(lowCandidate, closeText) > 0 ? closeText : lowCandidate,
        config.tickSize,
      );
      const openText = sameBucket
        ? previousOpen
        : config.interval === '1d'
          ? quantizePrice(tick.open, config.tickSize)
          : closeText;
      liveCandleRef.current = {
        time: bucketTimestamp,
        open: openText,
        high: highText,
        low: lowText,
      };
      const open = Number(openText);
      const high = Number(highText);
      const low = Number(lowText);
      const close = Number(closeText);
      const time = Math.floor(bucketTimestamp / 1000) as Time;
      candleSeries.update({ time, open, high, low, close });
      lineSeriesRef.current?.update({ time, value: close });
      volumeSeriesRef.current?.update({
        time,
        value: Number(tick.volume),
        color: close >= open ? '#3fd99a66' : '#ef777766',
      });
      window.dispatchEvent(new Event('market-chart-update-applied'));
    }, 150);
  }, [bars, interval, liveBar, tickSize]);

  useEffect(() => () => {
    if (liveTimerRef.current !== null) window.clearTimeout(liveTimerRef.current);
  }, []);

  useEffect(() => {
    candleSeriesRef.current?.applyOptions({ visible: chartMode === 'candles' });
    lineSeriesRef.current?.applyOptions({ visible: chartMode === 'line' });
    volumeSeriesRef.current?.applyOptions({ visible: enabledIndicators.includes('volume') });
    indicatorSeriesRef.current.forEach((series, key) => {
      series.applyOptions({
        visible: enabledIndicators.includes(key)
          || (key.startsWith('bb_') && enabledIndicators.includes('bb_upper'))
          || (key === 'macd_signal' && enabledIndicators.includes('macd')),
      });
    });
    macdHistogramRef.current?.applyOptions({
      visible: enabledIndicators.includes('macd'),
    });
  }, [chartMode, enabledIndicators]);

  return (
    <div className="candlestick-panel">
      <div className="candlestick-toolbar">
        <div className="candlestick-toggle" role="group" aria-label="Chart style">
          <button type="button" className={chartMode === 'candles' ? 'selected' : ''} onClick={() => setChartMode('candles')}>Candles</button>
          <button type="button" className={chartMode === 'line' ? 'selected' : ''} onClick={() => setChartMode('line')}>Line</button>
        </div>
        <div style={{ position: 'relative' }}>
          <button
            type="button"
            aria-expanded={indicatorsOpen}
            aria-haspopup="true"
            onClick={() => setIndicatorsOpen((open) => !open)}
          >
            Indicators <span aria-label={`${activeIndicatorCount} enabled`}>{activeIndicatorCount}</span>
          </button>
          {indicatorsOpen && (
            <div
              role="group"
              aria-label="Chart indicators"
              style={{
                position: 'absolute',
                zIndex: 10,
                top: 'calc(100% + 6px)',
                left: 0,
                display: 'grid',
                gap: 8,
                minWidth: 210,
                padding: 12,
                border: '1px solid #303b49',
                borderRadius: 6,
                background: '#111821',
                boxShadow: '0 10px 24px #0008',
              }}
            >
              {availableIndicators.map((indicator) => (
                <label
                  key={indicator.key}
                  title={!indicator.available
                    ? `Insufficient stored history: ${indicator.label} requires ${indicator.period} bars; ${fullSeriesBarsAvailable} available.`
                    : undefined}
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    gap: 8,
                    color: indicator.available ? '#d3dce6' : '#7d8996',
                    fontSize: 11,
                  }}
                >
                  <input
                    type="checkbox"
                    checked={indicator.available && enabledIndicators.includes(indicator.key)}
                    disabled={!indicator.available}
                    onChange={() => setEnabledIndicators((current) => (
                      current.includes(indicator.key)
                        ? current.filter((key) => key !== indicator.key)
                        : [...current, indicator.key]
                    ))}
                  />
                  <span>{indicator.label}</span>
                </label>
              ))}
            </div>
          )}
        </div>
        <div className="crosshair-readout" aria-live="polite">
          {readout
            ? `${readout.time} · O ${price(readout.open, currency)} H ${price(readout.high, currency)} L ${price(readout.low, currency)} C ${price(readout.close, currency)}`
            : 'Move crosshair over chart for OHLC'}
        </div>
      </div>
      <div className="ohlc-chart" ref={containerRef} aria-label={`${interval} chart with price, volume, and indicator panes`} />
      {loadingOlder && <p className="loading-inline">Loading earlier candles…</p>}
      {!hasOlder && bars.length > 0 && <p className="market-provenance">No earlier candles are available from the selected provider.</p>}
    </div>
  );
}
