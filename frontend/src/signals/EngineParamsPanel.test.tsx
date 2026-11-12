// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ENGINE_GROUPS, EngineParamsPanel } from './EngineParamsPanel';
import { geometry } from './engineGeometry';

afterEach(cleanup);

// TEST DATA: stored defaults as the library entry carries them, and SBIN-like measured inputs from the geometry endpoint.
const STORED = { signal_types: 'T,R,B', bar_minutes: 5, H_minutes: 30, entry_mode: 'market', no_entry_hours_ist: '', r: 2.0, k_vol: 1.0, theta_feeR: 0.25, s_cap_mult: 2.0, must_work_frac: 0.3, must_work_at_frac: 0.3333, partial_pct: 0, risk_pct: 0.005 };
const GEO = { status: 'ready' as const, symbol: 'SBIN', inputs: { cost_rt_pct: 0.1063, sigma_H_median_pct: 0.285, typical_bar_pct: 0.175 } };

describe('EngineParamsPanel', () => {
  it('groups the thirteen parameters into the four named sections, in order, with units and stored defaults as placeholders', () => {
    render(<EngineParamsPanel stored={STORED} values={{}} onChange={() => undefined} geometry={GEO} />);
    const groups = screen.getAllByRole('group').filter((g) => g.tagName === 'FIELDSET');
    expect(groups.map((g) => g.querySelector('legend')?.textContent)).toEqual(['Signals & timing', 'Barrier geometry', 'Exit rules', 'Risk']);
    expect(ENGINE_GROUPS.flatMap((g) => g.params).map((p) => p.key).sort()).toEqual(Object.keys(STORED).sort());     // every stored key has a field, none extra
    expect(within(groups[1]).getByLabelText('engine r')).toHaveAttribute('placeholder', '2');
    expect(within(groups[0]).getByLabelText('engine H_minutes')).toHaveAttribute('placeholder', '30');
    expect(within(groups[0]).getByLabelText('engine signal_types')).toHaveAttribute('placeholder', 'T,R,B');
    expect(screen.getByText('Horizon H (min)')).toBeInTheDocument();
    expect(screen.getByText('Partial exit (%)')).toBeInTheDocument();
    expect(screen.getByText(/blank = stored value/)).toBeInTheDocument();
    // long explanations are in the info affordance, not body text
    expect(screen.getByRole('img', { name: /About s-cap multiplier/ })).toHaveAttribute('title', expect.stringMatching(/skipped when the floored stop/));
    expect(screen.queryByText(/skipped when the floored stop/)).toBeNull();
  });

  it('shows the effective geometry from stored values and recomputes it as values change', () => {
    const onChange = vi.fn();
    const { rerender } = render(<EngineParamsPanel stored={STORED} values={{}} onChange={onChange} geometry={GEO} />);
    const preview = () => within(screen.getByLabelText('Effective geometry'));
    expect(preview().getByText('0.1063%')).toBeInTheDocument();                       // round-trip cost
    expect(preview().getAllByText('0.4252%')).toHaveLength(3);                         // cost floor 0.1063 / 0.25; the implied stop equals it (floor wins); and the s-cap sigma_H threshold = floor x r / (k_vol x cap) = same
    expect(preview().getByText('0.8504%')).toBeInTheDocument();                       // target 2 x 0.4252
    expect(preview().getByText('41.7%')).toBeInTheDocument();                         // p* = (1 + 0.25) / 3
    expect(preview().getByText(/≈ 2.4 typical bars/)).toBeInTheDocument();            // stop 0.4252 / 0.175
    expect(preview().getByText(/≈ 1.5 typical bars in 2 bars/)).toBeInTheDocument();  // must-work 0.2551 / 0.175 within ceil(10/5)
    expect(preview().getByText(/a typical signal is skipped/)).toBeInTheDocument();   // floor 0.4252 > 2 x (0.285/2)
    fireEvent.change(screen.getByLabelText('engine theta_feeR'), { target: { value: '0.5' } });
    expect(onChange).toHaveBeenCalledWith({ theta_feeR: '0.5' });
    rerender(<EngineParamsPanel stored={STORED} values={{ theta_feeR: '0.5', H_minutes: '120' }} onChange={onChange} geometry={GEO} />);
    expect(preview().getAllByText('0.2126%').length).toBeGreaterThanOrEqual(1);         // floor halves
    expect(preview().getByText(/in 8 bars/)).toBeInTheDocument();                     // must-work check at 120/3 = 40 min = 8 bars
    expect(preview().getByText(/a typical signal trades/)).toBeInTheDocument();       // 0.2126 <= 2 x 0.1425
  });

  it('renders dashes, never zeros, when the instrument cannot be measured', () => {
    render(<EngineParamsPanel stored={STORED} values={{}} onChange={() => undefined} geometry={{ status: 'unavailable', inputs: null, reason: 'no stored 1-minute bars in this window' }} />);
    const preview = within(screen.getByLabelText('Effective geometry'));
    expect(preview.getByText(/Not measurable: no stored 1-minute bars/)).toBeInTheDocument();
    expect(preview.getAllByText('—').length).toBeGreaterThanOrEqual(5);
    expect(preview.queryByText('0.0000%')).toBeNull();
  });
});

describe('geometry()', () => {
  it('matches the engine formulas', () => {
    const g = geometry({ r: 2, k_vol: 1, theta_feeR: 0.25, s_cap_mult: 2, must_work_frac: 0.3, must_work_at_frac: 1 / 3, H_minutes: 30, bar_minutes: 5 }, { cost_rt_pct: 0.1063, sigma_H_median_pct: 1.2, typical_bar_pct: 0.3 });
    expect(g.volStopPct).toBeCloseTo(0.6); expect(g.stopPct).toBeCloseTo(0.6);          // volatility stop wins over the floor
    expect(g.typicalCapped).toBe(false); expect(g.mustWorkBars).toBe(2);
    expect(g.pStar).toBeCloseTo((1 + 0.1063 / 0.6) / 3);
  });
});
