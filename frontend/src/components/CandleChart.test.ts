import { describe, expect, it } from 'vitest';
import type { UTCTimestamp } from 'lightweight-charts';
import {
  chartTimeZone,
  formatAxisTickLabel,
  formatBarLabel,
  formatChartTime,
  sessionCloseForVenue,
} from './CandleChart';

describe('chart daily labels', () => {
  // 2026-10-30 00:00 UTC is the stored key for the 30 Oct NYSE-zone session date.
  const dailyKey = (Date.UTC(2026, 9, 30) / 1000) as UTCTimestamp;

  it('labels daily bars in UTC so a US-zone instrument is not shifted back a day', () => {
    expect(chartTimeZone('1d', 'America/New_York')).toBe('UTC');
    const label = formatChartTime(dailyKey, chartTimeZone('1d', 'America/New_York'), false);
    expect(label).toContain('30');
    expect(label).not.toContain('29');
  });

  it('keeps the instrument timezone for intraday bars', () => {
    expect(chartTimeZone('5m', 'Asia/Kolkata')).toBe('Asia/Kolkata');
    expect(chartTimeZone('1h', 'America/New_York')).toBe('America/New_York');
  });

  it('keeps intraday axis ticks compact enough for the time scale to space them', () => {
    const intraday = (Date.UTC(2026, 10, 3, 9, 0) / 1000) as UTCTimestamp;
    const label = formatAxisTickLabel(intraday, '1m', 'Asia/Kolkata');
    expect(label).toMatch(/^Nov 03 14:30$/);
    expect(label.length).toBeLessThanOrEqual(12);
  });
});

describe('daily candle tooltip session close', () => {
  const dailyKey = (Date.UTC(2026, 9, 30) / 1000) as UTCTimestamp;
  const nse = sessionCloseForVenue('NSE');

  it('shows the IST session date with the NSE 3:30 PM close for a UTC-midnight key', () => {
    const label = formatBarLabel(dailyKey, '1d', 'Asia/Kolkata', nse);
    expect(label).toContain('Oct 30, 2026');
    expect(label).toContain('3:30');
    expect(label).toContain('PM');
    expect(label).toContain('IST');
  });

  it('does not shift the displayed date to October 29 or October 31', () => {
    const label = formatBarLabel(dailyKey, '1d', 'America/New_York', nse);
    expect(label).not.toContain('Oct 29');
    expect(label).not.toContain('Oct 31');
    expect(label).toContain('Oct 30');
  });

  it('keeps the tick-mark date-only for daily bars', () => {
    expect(formatBarLabel(dailyKey, '1d', 'Asia/Kolkata', nse, false)).toBe('Oct 30, 2026');
  });

  it('does not invent a time when session-close metadata is missing', () => {
    const label = formatBarLabel(dailyKey, '1d', 'Asia/Kolkata', null);
    expect(label).toBe(formatChartTime(dailyKey, 'UTC', false));
    expect(label).not.toContain(':');
    expect(label).not.toContain('IST');
  });

  it('leaves intraday timestamps in the instrument timezone without a session close', () => {
    const intraday = (Date.UTC(2026, 9, 30, 4, 0) / 1000) as UTCTimestamp;
    expect(formatBarLabel(intraday, '5m', 'Asia/Kolkata', nse)).toBe(
      formatChartTime(intraday, 'Asia/Kolkata'),
    );
    expect(formatBarLabel(intraday, '5m', 'Asia/Kolkata', nse)).not.toContain('IST');
  });

  it('maps only venues with known close metadata', () => {
    expect(sessionCloseForVenue('NSE')).toEqual({ hour: 15, minute: 30, zoneLabel: 'IST' });
    expect(sessionCloseForVenue('bse')).toEqual({ hour: 15, minute: 30, zoneLabel: 'IST' });
    expect(sessionCloseForVenue('NYSE')).toBeNull();
    expect(sessionCloseForVenue(null)).toBeNull();
  });
});
