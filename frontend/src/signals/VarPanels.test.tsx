// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../api', () => ({ apiRequest: vi.fn(), ApiRequestError: class extends Error {} }));
import { apiRequest } from '../api';
import { VarCharges } from './VarCharges';
import { VarFunnel } from './VarFunnel';
import { VarLedger } from './VarLedger';
import { VarPerformance } from './VarPerformance';

const api = vi.mocked(apiRequest);
beforeEach(() => { api.mockReset(); });
afterEach(cleanup);

// TEST DATA only: shapes mirror the /var API results; the numbers are arbitrary and mean nothing about any market.
const row = (stage: string, reason: string, instrument: string, tod: string, setup = 'ACCEPTANCE_BREAKOUT') => ({ stage, reason, instrument, session: '2026-11-02', tod_bucket: tod, setup, direction: 'LONG' });
const FUNNEL = { engine: 'VAR', reconciles: true, reconciliation: 'signals 2 = risk-rejected 0 + execution-rejected 1 + trades 1', candidates_generated: 5,
  stages: [{ stage: 'strategy_conditions', entered: 5, rejected: 3, passed: 2, first_failed: [], any_failed: [{ reason: 'regime', n: 3, pct_of_stage_rejected: 100 }] }],
  pattern_stages: { 'BO:acceptance:long_or_up': 9 },
  rows: [row('strategy', 'regime', 'NIFTY', '10:00'), row('strategy', 'regime', 'NIFTY', '10:30'), row('strategy', 'relative_volume', 'BANKNIFTY', '10:00'),
    row('signal', 'signal_generated', 'NIFTY', '11:00'), row('signal', 'signal_generated', 'BANKNIFTY', '11:00'), row('execution', 'entry_not_filled', 'NIFTY', '11:00'), row('trade', 'trade_executed', 'BANKNIFTY', '11:00')] };

describe('VarFunnel', () => {
  it('shows the reconciled funnel and recomputes it when filtered by instrument', () => {
    render(<VarFunnel result={{ funnel: FUNNEL }} />);
    expect(screen.getByText('reconciles')).toBeInTheDocument();
    const table = screen.getByRole('region', { name: 'Funnel' });
    expect(within(table).getByText('Candidates generated').closest('tr')).toHaveTextContent('5');
    fireEvent.change(screen.getByLabelText('Instrument'), { target: { value: 'BANKNIFTY' } });
    expect(within(table).getByText('Candidates generated').closest('tr')).toHaveTextContent('2');          // 1 strategy-rejected + 1 signal
    expect(screen.getByText(/Filtered view/)).toBeInTheDocument();
    expect(within(table).queryByText('regime')).not.toBeInTheDocument();
  });

  it('shows an empty state without a funnel', () => {
    render(<VarFunnel result={{}} />);
    expect(screen.getByText('No funnel for this run')).toBeInTheDocument();
  });
});

describe('VarPerformance', () => {
  it('replaces every performance figure with an insufficient-sample state below 100 trades', () => {
    const result = { summary: { trades: 12, sample: { state: 'insufficient_sample', message: 'Insufficient sample: 12 trades < 100.' }, configurations_evaluated: 4,
      diagnostics_only: { gross_pnl: '23085', total_charges: '14023.02', net_pnl: '9061.98', total_turnover: '1' } }, by_setup: { ACCEPTANCE_BREAKOUT: { trades: 10, sample: { state: 'insufficient_sample' } } } };
    render(<VarPerformance result={result} scheduleOk />);
    expect(screen.getByText(/Insufficient sample/, { selector: 'strong' })).toBeInTheDocument();
    expect(screen.queryByText('Win rate (net)')).not.toBeInTheDocument();
    expect(screen.queryByText('Net profit factor')).not.toBeInTheDocument();
    expect(screen.getByText(/Diagnostics only/)).toBeInTheDocument();
    expect(screen.getByText(/insufficient \(< 100\)/)).toBeInTheDocument();
    expect(screen.getByText(/configurations evaluated in this run: 4/)).toBeInTheDocument();
  });

  it('shows net as primary with t-stat, SE and CI once the sample is sufficient, and withholds net when the schedule is unavailable', () => {
    const s = { trades: 120, sample: { state: 'ok' }, net_pnl: '5000.00', gross_pnl: '9000.00', total_charges: '4000.00', win_rate_net_pct: 41.2, net_profit_factor: 1.2, gross_profit_factor: 1.5,
      net_expectancy_per_trade: '41.67', gross_expectancy_per_trade: '75.00', max_drawdown_net: '800', total_turnover: '9000000', avg_hold_minutes: 40, configurations_evaluated: 1,
      net_per_trade_stats: { n: 120, mean: 41.67, se: 30, t_stat: 1.39, ci95: [-17.8, 101.1] } };
    const { rerender } = render(<VarPerformance result={{ summary: s }} scheduleOk />);
    expect(screen.getByText('Net P&L (primary)').nextSibling).toHaveTextContent('5,000.00');
    expect(screen.getByText('t-statistic').nextSibling).toHaveTextContent('1.39');
    expect(screen.getByText(/95% confidence interval/).nextSibling).toHaveTextContent('-17.80 to 101.10');
    rerender(<VarPerformance result={{ summary: s }} scheduleOk={false} />);
    expect(screen.getByText(/Fee schedule unavailable/)).toBeInTheDocument();
    expect(screen.getByText('Net P&L (primary)').nextSibling).toHaveTextContent('—');
  });
});

describe('VarCharges', () => {
  const sched = { schedule_version: 'IN-NSE-DHAN-2026.11', broker: 'dhan', exchange: 'NSE', segment: 'futures', instrument_type: 'index_future', as_of: '2026-11-02', last_verified: '2026-11-17', rounding_method: 'r',
    components: { stt: { rate: '0.05', unit: 'percent of turnover', effective_from: '2026-04-01', status: 'verified_secondary', source: 'https://x', note: '' },
      stamp: { rate: '0.002', unit: 'percent of turnover', effective_from: '2024-10-01', status: 'owner_stated', source: 'https://y', note: '' } } };

  it('lists the schedule and flags components that are not primary-verified', async () => {
    api.mockResolvedValue(sched);
    render(<VarCharges segment="index_futures" asOf="2026-11-02" />);
    expect(await screen.findByText('IN-NSE-DHAN-2026.11')).toBeInTheDocument();
    expect(screen.getByText('owner-stated, unverified')).toBeInTheDocument();
    expect(screen.getByText(/not verified against a primary source: stt .*stamp/)).toBeInTheDocument();
    expect(api.mock.calls[0][0]).toContain('segment=futures');
  });

  it('shows an explicit unavailable state and never a valid-looking schedule when the backend refuses', async () => {
    api.mockRejectedValue(new Error('no declared rate for STT on 2024-01-01'));
    render(<VarCharges segment="equity_cash" />);
    expect(await screen.findByText(/Fee schedule unavailable: no declared rate/)).toBeInTheDocument();
    expect(screen.queryByText('IN-NSE-DHAN-2026.11')).not.toBeInTheDocument();
  });
});

describe('VarLedger', () => {
  const t = (over: Record<string, unknown>) => ({ date: '2026-11-02', instrument: 'NIFTY-Nov2026-FUT', setup: 'ACCEPTANCE_BREAKOUT', direction: 'LONG', regime: 'BULLISH_TREND', entry_ts: '2026-11-02T05:00:00+00:00', exit_ts: '2026-11-02T06:00:00+00:00',
    entry: '24000.00', lots: 1, lot_size: 65, gross_pnl: '1000', brokerage: '40', exchange_charge: '61.97', ipft: '0', sebi: '3.39', stt: '848', stamp_duty: '34', gst: '18.96', charges: '1006.32', net_pnl: '-6.32', r_multiple: -0.01,
    exit_reason: 'target', conditions: [['regime', 'pass', 'BULLISH'], ['relative_volume', 'fail', 'too low']], derivation: { atr_5m: 12.5 }, fee_schedule_version: 'V', unverified_components: ['stamp'],
    charge_orders: [{ order_id: 'entry', side: 'buy', turnover: '1560000', units: '65', rounded: { brokerage: '20', exchange: '28.6', ipft: '0', sebi: '1.56', stt: '0', stamp: '31', gst: '8.7' }, total: '89.9' },
      { order_id: 'exit1', side: 'sell', turnover: '1565000', units: '65', rounded: { brokerage: '20', exchange: '28.7', ipft: '0', sebi: '1.57', stt: '783', stamp: '0', gst: '8.7' }, total: '841.9' }], ...over });
  const trades = [t({ instrument: 'A-FUT', net_pnl: '500', gross_pnl: '900' }), t({ instrument: 'B-FUT', direction: 'SHORT', net_pnl: '-200' }), t({ instrument: 'C-FUT', net_pnl: '100', date: '2026-11-04' })];

  it('filters, sorts and opens per-condition diagnostics with the per-order charge breakdown', () => {
    render(<VarLedger trades={trades} scheduleOk />);
    expect(screen.getByText('3 of 3 trades')).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Net P/L'), { target: { value: 'win' } });
    expect(screen.getByText('2 of 3 trades')).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Direction'), { target: { value: 'SHORT' } });
    expect(screen.getByText('0 of 3 trades')).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Direction'), { target: { value: '' } });
    fireEvent.click(screen.getByRole('button', { name: /^Net/ }));
    fireEvent.click(screen.getByRole('button', { name: /^Net/ }));                      // descending
    const first = screen.getAllByRole('row')[1];
    expect(first).toHaveTextContent('A-FUT');
    fireEvent.click(first);
    const dlg = screen.getByRole('dialog', { name: 'Trade diagnostics' });
    expect(within(dlg).getByText(/too low/)).toBeInTheDocument();
    expect(within(dlg).getByText('exit1')).toBeInTheDocument();
    expect(within(dlg).getByText(/Unverified components: stamp/)).toBeInTheDocument();
  });

  it('shows an empty state with no trades and warns when the schedule is unavailable', async () => {
    const { rerender } = render(<VarLedger trades={[]} scheduleOk />);
    expect(screen.getByText('No trades')).toBeInTheDocument();
    rerender(<VarLedger trades={trades} scheduleOk={false} />);
    await waitFor(() => expect(screen.getByText(/net figures in this ledger are not valid/)).toBeInTheDocument());
  });
});
