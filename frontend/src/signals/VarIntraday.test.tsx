// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../api', () => ({ apiRequest: vi.fn(), ApiRequestError: class extends Error {} }));
import { apiRequest } from '../api';
import { VarIntraday } from './VarIntraday';

const api = vi.mocked(apiRequest);
beforeEach(() => { api.mockReset(); });
afterEach(cleanup);

// TEST DATA only: shapes mirror the /var API responses; no market values are asserted as real.
const CAPS = {
  futures_candles_1m: [{ underlying: 'NIFTY', type: 'FUTIDX', contracts: 3, bars: 100, first: '2026-08-26', last: '2026-11-05', sessions: 49 }],
  stock_futures: { underlyings_with_candles: 1, bars: 10 }, stored_ticks: { rows: 5, first: null, last: null, fields: 'snapshot OHLC + cumulative volume only' },
  data_level: 'C_candle_approximate', supports: ['volume profile (approximate, candle-distributed)'], does_not_support: ['bid/ask-classified or tick-rule delta'],
  order_flow: 'unavailable: no stored last-traded quantity, bid/ask or depth', volume_unit: 'underlying units', tick_size_unit: 'paise', lot_size_note: 'lot note', session_note: 'session note', disclaimer: '',
};
const CFG = { version: '1.0.0', engines: ['breakout', 'reversal'], variant: 'A', exits: { exit_model: 'fixed_partial', partial_booking_pct: 50, time_stop_bars: 6 }, risk: { risk_per_trade_pct: 0.25, max_daily_loss_pct: 0.5, max_entries_per_session: 3, min_net_reward_to_cost: 1 },
  market_structure: { value_area_pct: 70 }, breakout: { minimum_outside_volume_pct: 25, delta_ratio_minimum: 0.15, relative_volume_minimum: 1.2, minimum_rr: 1.5 },
  reversal: { rejection_delta_ratio: 0.1, minimum_rr: 1.5 }, regime: { slope_threshold: 0.05 } };

const PRE = [{ symbol: 'SBIN', status: 'ok', sessions_available: 200, sessions_usable: 180, reason: '', warnings: [] }, { symbol: 'ITC', status: 'not_in_storage', sessions_available: 0, sessions_usable: 0, reason: 'no instrument / contract with this symbol is stored', warnings: [] }];

function route(runs: unknown[] = []) {
  api.mockImplementation(async (path: string, init?: { method?: string }) => {
    if (path === '/var/data-capabilities') return CAPS;
    if (path === '/var/config' && !init?.method) return { config: CFG, version_no: 0 };
    if (path === '/var/config') return { version_no: 1 };
    if (path === '/var/backtests' && init?.method === 'POST') return { id: 'r1' };
    if (path === '/var/backtests') return runs;
    if (path.startsWith('/var/preflight')) return { symbols: PRE };
    if (path === '/var/backtests/r1') return { id: 'r1', status: 'unavailable', created_at: '2026-11-17T00:00:00Z', finished_at: null, error: null, request: {}, config_snapshot: {}, disclaimer: '', result: { status: 'unavailable', reason: 'no stored futures candles for the requested underlyings' } };
    throw new Error(`unexpected ${path}`);
  });
}

describe('VarIntraday', () => {
  it('labels the work research-only and shows order flow as unavailable rather than zero', async () => {
    route();
    render(<VarIntraday />);
    expect(await screen.findByText(/not investment advice/i)).toBeInTheDocument();
    expect(await screen.findByText('Order flow: unavailable')).toBeInTheDocument();
    expect(screen.getAllByText(/no stored last-traded quantity/i).length).toBeGreaterThan(0);
    fireEvent.click(screen.getByRole('button', { name: 'Config & run' }));
    expect(await screen.findByText('No backtests yet')).toBeInTheDocument();
  });

  it('warns that order-flow variants will not trade on this data', async () => {
    route();
    render(<VarIntraday />);
    fireEvent.click(await screen.findByRole('button', { name: 'Config & run' }));
    const select = await screen.findByLabelText(/^Variant/);
    fireEvent.change(select, { target: { value: 'B' } });
    expect(await screen.findByText(/require order flow, which is not stored/i)).toBeInTheDocument();
  });

  it('saves settings as a new version and reports it', async () => {
    route();
    render(<VarIntraday />);
    fireEvent.click(await screen.findByRole('button', { name: 'Config & run' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Save as new version' }));
    expect(await screen.findByText(/Saved configuration version 1/)).toBeInTheDocument();
  });

  it('shows an explicit unavailable state for a run without stored data', async () => {
    route([{ id: 'r1', status: 'unavailable', created_at: '2026-11-17T00:00:00Z', finished_at: null, error: null, request: { underlyings: ['XYZ'] }, config_snapshot: {}, result: null, disclaimer: '' }]);
    render(<VarIntraday />);
    fireEvent.click(await screen.findByRole('button', { name: 'Config & run' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Open' }));
    await waitFor(() => expect(screen.getByText(/did not replay any data/)).toBeInTheDocument());
    expect(screen.getAllByText(/Not completed: unavailable/).length).toBeGreaterThan(0);
    expect(screen.queryByText(/Performance/, { selector: 'h2' })).not.toBeInTheDocument();
  });

  it('checks symbols against stored data before launch and disables the run when none is usable or unusable ones are not opted out', async () => {
    route();
    render(<VarIntraday />);
    fireEvent.click(await screen.findByRole('button', { name: 'Config & run' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Check data first' }));
    expect(await screen.findByText('not in storage')).toBeInTheDocument();
    const run = screen.getByRole('button', { name: 'Run backtest' });
    expect(run).toBeDisabled();                                                     // ITC has no stored data and partial runs are not opted into
    fireEvent.click(screen.getByLabelText(/Run only the 1 usable symbol/));
    expect(run).not.toBeDisabled();
  });

  it('renders a no_usable_data run as a failure with its sessions loaded vs excluded and NO performance panel', async () => {
    const audit = [{ underlying: 'RELIANCE', sessions_available: 500, sessions_loaded: 0, sessions_loaded_in_window: 0, sessions_excluded: 500, excluded_by_reason: { truncated_or_incomplete_session: 500 }, first_loaded: null, last_loaded: null, notes: [],
      rvol_baseline: { can_complete: false } }];
    route([{ id: 'r1', status: 'no_usable_data', created_at: '2026-11-17T00:00:00Z', finished_at: null, error: null, request: { underlyings: ['RELIANCE'], segment: 'equity_cash' }, config_snapshot: {}, disclaimer: '',
      result: { status: 'no_usable_data', reason: 'no session of any requested symbol could be loaded', data_audit: audit } }]);
    api.mockImplementation(async (path: string, init?: { method?: string }) => {
      if (path === '/var/data-capabilities') return CAPS;
      if (path === '/var/config') return { config: CFG, version_no: 0 };
      if (path === '/var/backtests' && !init?.method) return [{ id: 'r1', status: 'no_usable_data', created_at: '2026-11-17T00:00:00Z', finished_at: null, error: null, request: { underlyings: ['RELIANCE'], segment: 'equity_cash' }, config_snapshot: {}, disclaimer: '', result: null }];
      if (path === '/var/backtests/r1') return { id: 'r1', status: 'no_usable_data', created_at: '2026-11-17T00:00:00Z', finished_at: null, error: null, request: { underlyings: ['RELIANCE'], segment: 'equity_cash' }, config_snapshot: {}, disclaimer: '',
        result: { status: 'no_usable_data', reason: 'no session of any requested symbol could be loaded', data_audit: audit } };
      throw new Error(`unexpected ${path}`);
    });
    render(<VarIntraday />);
    fireEvent.click(await screen.findByRole('button', { name: 'Config & run' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Open' }));
    expect(await screen.findByText('No usable data.')).toBeInTheDocument();
    expect(screen.getByText(/500 × truncated/)).toBeInTheDocument();
    expect(screen.getByText('cannot complete')).toBeInTheDocument();
    expect(screen.getByText(/Not completed: no usable data/)).toBeInTheDocument();
    expect(screen.queryByText('Net P&L (primary)')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Performance')).not.toBeInTheDocument();
  });
});
