import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { StrategyRegistry, type RegistryStrategy } from './StrategyRegistry';
import type { StrategyRow } from './types';

const calls: { path: string; method: string }[] = [];
let installed = false;

const items = (): RegistryStrategy[] => [
  { key: 'alpha', name: 'Alpha Trend', family: 'trend', summary: 'Trades trend flips.', rationale: 'Trends persist; fails in chop.', version: '1', installed_strategy_id: installed ? 's-alpha' : null,
    params: [{ name: 'adx_min', default: 22, min: 10, max: 40, description: 'minimum ADX' }, { name: 'rr', default: 2, min: 1, max: 5, description: 'target multiple' }],
    evidence: { symbols: ['TCS', 'INFY'], timeframe: '30m', bars: 187_500, data_window: { start: '2025-11-10T00:00:00Z', end: '2026-11-20T00:00:00Z' }, cost_profile: 'Dhan · NSE · Intraday', trades: 311,
      gross_pnl_before_costs: '13535.00', total_charges: '25592.00', total_slippage: '12256.00', net_profit: '-24313.00', profit_factor_net: 0.61, symbols_profitable: 1, symbols_tested: 6,
      out_of_sample: { net_profit: '-9000.00', profit_factor_net: 0.5 }, verdict: 'Did not clear costs.', note: 'Research only.' } },
  { key: 'beta', name: 'Beta Reversion', family: 'mean_reversion', summary: 'Fades stretches.', rationale: 'Mean reverts.', version: '1', installed_strategy_id: installed ? 's-beta' : null, params: [], evidence: null },
];

vi.mock('./lab', async (importOriginal) => {
  const original = await importOriginal<typeof import('./lab')>();
  return {
    ...original,
    lab: async (path: string, init: { method?: string } = {}) => {
      calls.push({ path, method: init.method ?? 'GET' });
      if (path === '/registry') return { strategies: items() };
      if (path === '/starters/install') { installed = true; return { installed: 2, skipped: [] }; }
      throw new Error(`unexpected ${path}`);
    },
  };
});

const owned = [{ id: 's-alpha', name: 'Registry: Alpha Trend', kind: 'intraday' } as unknown as StrategyRow];
beforeEach(() => { calls.length = 0; installed = false; });
afterEach(cleanup);

describe('StrategyRegistry', () => {
  it('lists strategies with family, rationale, parameters and the hypothetical label', async () => {
    render(<StrategyRegistry strategies={[]} onInstalled={vi.fn()} onOpen={vi.fn()} goLab={vi.fn()} />);
    const alpha = within(await screen.findByRole('article', { name: 'Alpha Trend' }));
    expect(alpha.getByText('Trend')).toBeTruthy();
    expect(alpha.getByText(/Trends persist; fails in chop/)).toBeTruthy();
    fireEvent.click(alpha.getByText('2 parameters'));
    expect(alpha.getByText('adx_min')).toBeTruthy();
    expect(alpha.getByText('10–40')).toBeTruthy();
    expect(screen.getByText(/hypothetical backtests/i)).toBeTruthy();
  });

  it('shows the recorded evidence with gross, charges, slippage and net, including the out-of-sample result', async () => {
    render(<StrategyRegistry strategies={[]} onInstalled={vi.fn()} onOpen={vi.fn()} goLab={vi.fn()} />);
    const ev = within(await screen.findByLabelText('Alpha Trend recorded result'));
    expect(ev.getByText(/Recorded result \(hypothetical\)/)).toBeTruthy();
    expect(ev.getByText(/2 symbols · 30m · 2025-11-10 → 2026-11-20 · 1,87,500 1-minute bars/)).toBeTruthy();
    expect(ev.getByText('311')).toBeTruthy();
    expect(ev.getByText('25,592.00')).toBeTruthy();                                    // charges are never hidden
    expect(ev.getByText('0.61')).toBeTruthy();
    expect(ev.getByText('1/6')).toBeTruthy();
    expect(ev.getByText('Out-of-sample net')).toBeTruthy();
    expect(ev.getByText('Did not clear costs.')).toBeTruthy();                         // the verdict is stated plainly, favourable or not
    expect(within(screen.getByRole('article', { name: 'Beta Reversion' })).getByText(/Not tested/)).toBeTruthy();
  });

  it('filters by family and installs the missing strategies into the library', async () => {
    const onInstalled = vi.fn();
    render(<StrategyRegistry strategies={[]} onInstalled={onInstalled} onOpen={vi.fn()} goLab={vi.fn()} />);
    await screen.findByRole('article', { name: 'Alpha Trend' });
    fireEvent.change(screen.getByLabelText('Family'), { target: { value: 'mean_reversion' } });
    expect(screen.queryByRole('article', { name: 'Alpha Trend' })).toBeNull();
    expect(screen.getByRole('article', { name: 'Beta Reversion' })).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Install 2 into my library' }));
    expect(await screen.findByText(/Installed 2 strategy\(ies\) into your library/)).toBeTruthy();
    expect(calls.some((c) => c.path === '/starters/install' && c.method === 'POST')).toBe(true);
    await waitFor(() => expect(onInstalled).toHaveBeenCalled());
    expect(screen.getByRole('button', { name: 'All installed' }).hasAttribute('disabled')).toBe(true);
  });

  it('offers Open in Builder and Run in Backtest Lab only for installed strategies', async () => {
    installed = true;
    const onOpen = vi.fn();
    const goLab = vi.fn();
    render(<StrategyRegistry strategies={owned} onInstalled={vi.fn()} onOpen={onOpen} goLab={goLab} />);
    const alpha = within(await screen.findByRole('article', { name: 'Alpha Trend' }));
    fireEvent.click(alpha.getByRole('button', { name: 'Open in Builder' }));
    expect(onOpen).toHaveBeenCalledWith(owned[0]);
    fireEvent.click(alpha.getByRole('button', { name: 'Run in Backtest Lab' }));
    expect(goLab).toHaveBeenCalled();
    expect(within(screen.getByRole('article', { name: 'Beta Reversion' })).queryByRole('button', { name: 'Open in Builder' })).toBeNull();
    expect(within(screen.getByRole('article', { name: 'Beta Reversion' })).getByText('Install to edit or backtest.')).toBeTruthy();
  });
});
