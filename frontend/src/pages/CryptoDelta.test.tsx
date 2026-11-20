import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { CryptoDeltaPage } from './CryptoDelta';

vi.mock('./DeltaResearch', () => ({ DeltaResearchPage: () => <div>delta research stub</div> }));

const status = {
  mode: { configured: 'research', effective: 'research', live_blocked: false, reason: null },
  capabilities: { order_transmission: false, live_adapter: false, execution_credentials: 'not stored; broker access is read-only', simulated_fills: 'backtest only' },
  limits: { currency: 'USD', max_order_notional: '1000', max_total_exposure: '5000', max_price_age_seconds: 120 },
  audit_events: 0,
  disclaimer: 'Decision support, not investment advice.',
};
const dryRun = {
  mode: 'research',
  client_order_id: 'tc0123456789abcdef0123456789ab',
  decision: {
    approved: false, notional: '120.000', currency: 'USD', reasons: ['exposure_verifiable: unverifiable'],
    checks: [
      { name: 'price_fresh', passed: true, detail: 'price is 3s old' },
      { name: 'exposure_verifiable', passed: false, detail: 'current exposure is unverifiable' },
    ],
  },
  order_transmission: 'not_available',
  note: 'An approved decision means the risk checks would pass. It is not an order and none is sent.',
};

function mockApi() {
  const calls: { url: string; init?: RequestInit }[] = [];
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    calls.push({ url, init });
    const body = url.includes('/execution/status') ? status
      : url.includes('/execution/audit') ? []
        : url.includes('/execution/dry-run') ? dryRun : {};
    return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } });
  }));
  return calls;
}

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe('CryptoDeltaPage', () => {
  it('states plainly that orders are not placed and shows the guard-rail limits', async () => {
    mockApi();
    render(<CryptoDeltaPage />);
    expect(await screen.findByText('Orders are not placed from this app')).toBeTruthy();
    expect(await screen.findByText('Not available')).toBeTruthy();
    expect(screen.getByText('1000 USD')).toBeTruthy();
    expect(screen.getByText('delta research stub')).toBeTruthy();
  });

  it('runs a risk check and shows blocked results with the unverifiable exposure reason', async () => {
    const calls = mockApi();
    render(<CryptoDeltaPage />);
    await screen.findByText('Orders are not placed from this app');
    fireEvent.change(screen.getByLabelText(/Limit price/), { target: { value: '60000' } });
    fireEvent.click(screen.getByRole('button', { name: 'Run risk check' }));
    expect(await screen.findByText(/Blocked by the risk checks/)).toBeTruthy();
    expect(screen.getByText('✕ Current exposure verifiable')).toBeTruthy();
    const post = calls.find((call) => call.url.includes('/execution/dry-run'));
    const sent = JSON.parse(String(post?.init?.body)).intent;
    expect(sent).toMatchObject({ symbol: 'BTCUSD', side: 'buy', order_type: 'limit', limit_price: '60000', quantity: '1' });
    await waitFor(() => expect(typeof sent.quantity).toBe('string')); // numbers are sent as strings, never floats
  });
});
