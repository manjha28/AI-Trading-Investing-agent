import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { tradeExitMarker } from '../components/TerminalChart';
import { TradePlot } from './TradePlot';
import type { Trade } from './types';

vi.mock('../components/TerminalChart', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../components/TerminalChart')>();
  return {
    ...actual,
    TerminalChart: ({ candles, markers = [] }: { candles: unknown[]; markers?: { text: string; kind: string }[] }) => (
      <div data-testid="chart">{candles.length} candles · markers {markers.map((m) => m.text || '○').join(',')}</div>
    ),
  };
});

const trade = (over: Partial<Trade>): Trade => ({
  instrument_id: 'i1', symbol: 'TCS', side: 'long', qty: '10', signal_time: '', entry_time: '2026-11-18T04:00:00Z', exit_time: '2026-11-18T05:00:00Z',
  entry_raw: '100', entry_fill: '100.05', exit_raw: '105', exit_fill: '104.95', exit_reason: 'target', stop_initial: '98', target: '105',
  gross_pnl: '49', slippage_cost: '1', charges: '3', net_pnl: '45', r_multiple: '2', holding_minutes: 60, ambiguous_stop_and_target_same_bar: false, explain: null, ...over,
});
const trades: Trade[] = [
  trade({}),                                                                                                 // 18 Nov: long, target hit
  trade({ side: 'short', entry_time: '2026-11-18T06:00:00Z', exit_time: '2026-11-18T07:00:00Z', exit_reason: 'stop', net_pnl: '-20' }), // 18 Nov: short, stopped
  trade({ entry_time: '2026-11-19T04:00:00Z', exit_time: '2026-11-19T09:45:00Z', exit_reason: 'square_off', net_pnl: '5' }),             // 19 Nov
  trade({ instrument_id: 'i2', symbol: 'INFY', entry_time: '2026-11-19T04:30:00Z', exit_time: '2026-11-19T05:00:00Z', exit_reason: 'signal' }),
];

function mockBars(count = 3) {
  const calls: { url: string; method: string }[] = [];
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    calls.push({ url: String(url), method: (init?.method ?? 'GET').toUpperCase() });
    const bars = Array.from({ length: count }, (_, n) => ({ timestamp: `2026-11-18T0${n}:00:00Z`, open: '1', high: '2', low: '1', close: '2', volume: '5' }));
    return new Response(JSON.stringify({ bars }), { status: 200, headers: { 'Content-Type': 'application/json' } });
  }));
  return calls;
}

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe('tradeExitMarker', () => {
  it('labels stops, targets, signal exits and square-offs distinctly', () => {
    expect(tradeExitMarker('stop')).toEqual({ text: 'SL', outcome: 'stop' });
    expect(tradeExitMarker('trailing_stop')).toEqual({ text: 'SL', outcome: 'stop' });
    expect(tradeExitMarker('target')).toEqual({ text: 'T', outcome: 'target' });
    expect(tradeExitMarker('signal')).toEqual({ text: 'X', outcome: 'signal' });
    expect(tradeExitMarker('square_off')).toEqual({ text: 'EOD', outcome: 'other' });
    expect(tradeExitMarker('end_of_data').text).toBe('end');
  });
});

describe('TradePlot', () => {
  it('plots L/S entries and SL/T exits for the instrument with most trades, on its first trading day', async () => {
    const calls = mockBars();
    render(<TradePlot trades={trades} />);
    expect((await screen.findByTestId('chart')).textContent).toBe('3 candles · markers L,T,S,SL');
    // the IST trading day of 18 Nov: [17 Nov 18:30Z, 18 Nov 18:30Z), within the API's 500-bar limit
    const url = new URL(calls[0].url, 'http://x');
    expect(url.pathname).toContain('/instruments/i1/history');
    expect(url.searchParams.get('start')).toBe('2026-11-17T18:30:00.000Z');
    expect(url.searchParams.get('end')).toBe('2026-11-18T18:30:00.000Z');
    expect(url.searchParams.get('limit')).toBe('500');
    const table = screen.getByRole('table', { name: 'Trades on this day' });
    expect(within(table).getAllByRole('row')).toHaveLength(3); // header + the two trades of that day
    expect(within(table).getByText('target')).toBeTruthy();
    expect(within(table).getByText('stop')).toBeTruthy();
    const legend = screen.getByLabelText('Chart marker legend').textContent ?? '';
    for (const label of ['L long entry', 'S short entry', 'SL stop-loss hit', 'T target hit', 'X signal exit']) expect(legend).toContain(label);
  });

  it('moves between trading days and instruments, replotting that day only', async () => {
    mockBars();
    render(<TradePlot trades={trades} />);
    await screen.findByTestId('chart');
    fireEvent.click(screen.getByRole('button', { name: 'Next day ›' }));
    await waitFor(() => expect(screen.getByTestId('chart').textContent).toBe('3 candles · markers L,EOD'));
    expect(screen.getByRole('button', { name: 'Next day ›' })).toHaveProperty('disabled', true);
    fireEvent.change(screen.getByLabelText('Instrument'), { target: { value: 'i2' } });
    await waitFor(() => expect(screen.getByTestId('chart').textContent).toBe('3 candles · markers L,X'));
  });

  it('offers to load bars from the broker when none are stored, and says so plainly', async () => {
    const calls = mockBars(0);
    render(<TradePlot trades={trades} />);
    expect(await screen.findByText(/No stored bars for this day/)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Load from broker' }));
    await waitFor(() => expect(calls.some((c) => c.method === 'POST' && c.url.includes('/history/sync'))).toBe(true));
  });

  it('handles a run with no trades', () => {
    mockBars();
    render(<TradePlot trades={[]} />);
    expect(screen.getByText('This run has no trades to plot.')).toBeTruthy();
  });
});
