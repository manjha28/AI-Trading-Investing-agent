// @vitest-environment jsdom
import { cleanup, render, screen, within } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import { afterEach, describe, expect, it } from 'vitest';
import { ScalpHoldView, type ScalpHoldResult } from './ScalpHoldView';

afterEach(cleanup);

// TEST DATA only: the shape run_scalp_hold returns.
const RESULT: ScalpHoldResult = {
  kind: 'walk_forward', slip_bps: 2, unverified_assumptions: ['WARNING: slip_bps are ASSUMED, not measured: RPOWER=2.0'], total_combos_evaluated: 30, trades_total: 168,
  note: 'scalp_hold results are hypothetical research output on stored bars, never real P&L, and not investment advice.',
  refused_instruments: [{ symbol: 'SBIN', reason: 'only 120 usable 5-minute bars after excluding sessions with more than 5% missing candles' }],
  summary: { n: 168, mean_R: -0.1419, se_R: 0.061, fee_R_mean: 0.15 },
  trades: [{ instrument: 'RPOWER', ts_entry: '2026-01-05T04:00:00Z', side: 'long', signal_type: 'B', net_pnl: -12.5, R: -0.4 }],
  instruments: [{
    symbol: 'RPOWER', venue: 'NSE_EQUITY_MIS', bars_used: 35952, sessions_excluded: 1, sessions_total: 480, start: '2024-11-04T03:45:00+00:00', end: '2026-11-11T09:10:00+00:00',
    cost_report: { cost_rt_pct: 0.12, f_median: 0.09, p_star: 0.36 }, report: { n: 168, mean_R: -0.1419, exit_reasons: { stop: 0.3, target: 0.13 }, avgR_by_signal_type: { B: -0.14 } },
    folds: [{ fold: 1, combo: ['B', 2, 30, 1], n: 40, mean_R: -0.2, se_R: 0.1 }], warnings: ['fold 2: only 12 test trades (< 500); mean R is imprecise'],
    matched_random: { strategy_mean_R: -0.1419, percentile: 20, control_means: [-0.3, -0.2, -0.1, -0.15, -0.05] }, sign_flip: { n: 171, mean_R: -0.1655, se_R: 0.07 },
    phase0: [{ k_vol: 1, r: 2, H: 30, n: 2300, p_baseline: 0.0152 }], phase1: [{ signal: 'B', r: 2, H: 30, n: 50, edge: 0.01, t_stat: 0.4 }],
  }],
};

describe('ScalpHoldView', () => {
  it('leads with the hypothetical label, the unverified assumptions and mean R ± SE as the primary metric', () => {
    render(<ScalpHoldView r={RESULT} />);
    expect(screen.getByText(/hypothetical research output on stored bars, never real P&L/)).toBeInTheDocument();
    expect(screen.getByText(/slip_bps are ASSUMED, not measured/)).toBeInTheDocument();
    const stats = within(screen.getByLabelText('Key results'));
    expect(stats.getByText('Mean R per trade')).toBeInTheDocument();
    expect(stats.getAllByText('-0.142').length).toBeGreaterThan(0);
    expect(stats.getByText('Combinations evaluated')).toBeInTheDocument();
  });

  it('shows folds, null controls with the strategy percentile, per-fold imprecision warnings and refused instruments with their reason', () => {
    render(<ScalpHoldView r={RESULT} />);
    expect(screen.getByText(/sits at percentile/)).toHaveTextContent('percentile 20.0 of the controls');
    expect(screen.getByText(/Sign-flip \(every signal inverted\): mean R -0.166 over 171 trades/)).toBeInTheDocument();
    expect(screen.getByText(/only 12 test trades/)).toBeInTheDocument();
    expect(screen.getByText(/SBIN: only 120 usable 5-minute bars/)).toBeInTheDocument();
    expect(screen.getByText(/python -m scalp_hold phase3/)).toBeInTheDocument();        // bootstrap is CLI-only and says so
    expect(screen.getByText(/1 of 480 sessions excluded|1 of 480 sessions excluded for missing candles/)).toBeInTheDocument();
  });
});
