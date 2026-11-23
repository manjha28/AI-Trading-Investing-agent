import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { DhanReconciliation } from './DhanReconciliation';

const api = vi.hoisted(() => vi.fn());
vi.mock('../api', () => ({ apiRequest: api }));
afterEach(() => { cleanup(); api.mockReset(); });

const base = { day: '2026-11-23', trades_matched_that_day: 187, dhan: { realised: '4878.25', basis: 'gross', snapshot_at: '2026-11-23T10:00:00Z' },
  journal: { gross: '4878.27', fees_and_taxes_real: '0.00', fees_and_taxes_estimated: '4105.01', net: '773.26', trades_with_estimated_charges: 187, trades_with_unknown_charges: 0 },
  unexplained: '-0.02', reconciles_within_1_rupee: true, by_contract: [], open_lot_differences: [], contracts_without_master: [], notes: ['Dhan realised is gross.'] };

describe('DhanReconciliation', () => {
  it('shows Dhan gross, journal gross, real and estimated fees, net and the unexplained remainder, and warns that estimates are included', async () => {
    api.mockResolvedValue(base);
    render(<DhanReconciliation />);
    expect(await screen.findByText('reconciles')).toBeTruthy();
    expect(screen.getByText('₹4,878.25')).toBeTruthy(); expect(screen.getByText('₹4,105.01')).toBeTruthy(); expect(screen.getByText('₹773.26')).toBeTruthy(); expect(screen.getByText('-₹0.02')).toBeTruthy();
    expect(screen.getByText(/estimated charges/)).toBeTruthy();
  });
  it('flags a real gap and lists open-lot differences and contracts missing from the master', async () => {
    api.mockResolvedValue({ ...base, unexplained: '50.00', reconciles_within_1_rupee: false, journal: { ...base.journal, fees_and_taxes_estimated: '0.00' },
      open_lot_differences: [{ security_id: '999', journal_net_quantity: '65', dhan_net_quantity: '0' }],
      contracts_without_master: [{ symbol: 'NIFTY 27 OCT 22750 PUT', security_id: '51343', first_trade: '2026-10-27', reason: 'not in the stored Dhan master: the contract expired (expiry 27 Oct)' }] });
    render(<DhanReconciliation />);
    expect(await screen.findByText('unexplained gap')).toBeTruthy();
    expect(screen.getByText(/Open lots differ from Dhan \(1\)/)).toBeTruthy(); expect(screen.getByText(/Contracts missing from the Dhan master \(1\)/)).toBeTruthy();
    expect(screen.queryByText(/Part of the gap/)).toBeNull();
  });
  it('shows a real error with Retry, never a blank panel', async () => {
    api.mockRejectedValue(new Error('HTTP 503'));
    render(<DhanReconciliation />);
    expect((await screen.findByRole('alert')).textContent).toContain('HTTP 503');
  });
});
