import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import { DeltaResearchPage } from './DeltaResearch';

class TestEventSource extends EventTarget {
  close = vi.fn();
}

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  });
}

describe('DeltaResearchPage', () => {
  beforeEach(() => {
    vi.stubGlobal('EventSource', TestEventSource);
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('states clearly when public collection is disabled', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({
      enabled: false,
      milestone: 2,
      mode: 'public_research_read_only',
      store_status: 'disabled',
      store_modified_at: null,
      store_age_seconds: null,
      live_feeds: 'disabled',
      account_monitor: 'disabled',
      warnings: ['Delta research is disabled.'],
      disclaimer: 'Decision support, not investment advice.',
      feed: {
        state: 'disabled',
        connected: false,
        reconnects: 0,
        consecutive_failures: 0,
        dropped_events: 0,
        last_event_at: null,
        last_error: null,
      },
      collection_interval_seconds: 60,
    })));

    render(<DeltaResearchPage />);

    expect(await screen.findByText('Disabled')).toBeInTheDocument();
    expect(screen.getByText('Delta research is disabled.')).toBeInTheDocument();
  });

  it('renders collected public prices with their source and timestamp', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path.endsWith('/delta-research/status')) {
        return jsonResponse({
          enabled: true,
          milestone: 2,
          mode: 'public_research_read_only',
          store_status: 'present',
          store_modified_at: '2026-04-20T10:00:00Z',
          store_age_seconds: 1,
          live_feeds: 'connected',
          account_monitor: 'disabled',
          warnings: [],
          disclaimer: 'Decision support, not investment advice.',
          feed: {
            state: 'connected',
            connected: true,
            reconnects: 0,
            consecutive_failures: 0,
            dropped_events: 0,
            last_event_at: '2026-04-20T10:00:00Z',
            last_error: null,
          },
          collection_interval_seconds: 60,
        });
      }
      if (path.endsWith('/delta-research/overview')) {
        return jsonResponse({
          summary: {
            products: 1,
            quotes: 1,
            candles: 0,
            market_events: 0,
            latest_quote_at: '2026-04-20T10:00:00Z',
            latest_candle_at: null,
          },
        });
      }
      if (path.includes('/delta-research/markets?')) {
        return jsonResponse({
          items: [{
            product_id: '123',
            symbol: 'BTCUSD',
            contract_type: 'perpetual_futures',
            state: 'live',
            quote_time: '2026-04-20T10:00:00Z',
            last_price: '65000.25',
            mark_price: null,
            index_price: null,
            volume_24h: null,
            quote_source: 'delta_rest_ticker',
          }],
        });
      }
      return jsonResponse({ items: [] });
    }));

    render(<DeltaResearchPage />);
    await userEventClick(await screen.findByRole('button', { name: 'Markets' }));

    expect(await screen.findByText('BTCUSD')).toBeInTheDocument();
    expect(screen.getByText('65000.25')).toBeInTheDocument();
    expect(screen.getByText('delta_rest_ticker')).toBeInTheDocument();
  });
});

async function userEventClick(element: HTMLElement): Promise<void> {
  element.click();
  await waitFor(() => expect(element).toHaveAttribute('aria-pressed', 'true'));
}
