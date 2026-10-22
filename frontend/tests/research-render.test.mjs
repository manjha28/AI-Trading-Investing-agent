import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { fileURLToPath } from 'node:url';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import react from '@vitejs/plugin-react';
import { createServer } from 'vite';

let ResearchBriefView;
let vite;

before(async () => {
  vite = await createServer({
    configFile: false,
    root: fileURLToPath(new URL('../', import.meta.url)),
    plugins: [react()],
    optimizeDeps: { noDiscovery: true, entries: [] },
    server: { middlewareMode: true },
    appType: 'custom',
  });
  ({ ResearchBriefView } = await vite.ssrLoadModule('/src/pages/Research.tsx'));
});

after(async () => {
  await vite?.close();
});

test('Research brief reports usable partial data and hides provenance by default', () => {
  const brief = {
    run_id: 'run-1',
    instrument_id: 'instrument-1',
    status: 'partial',
    started_at: '2026-10-01T00:00:00Z',
    finished_at: '2026-10-01T00:00:01Z',
    sources_attempted: 1,
    sources_succeeded: 0,
    failures: [{
      source: 'Issuer news',
      stage: 'robots',
      url: 'https://example.com/news.xml',
      reason: 'Source disallowed by robots.txt.',
    }],
    renderer: 'template',
    created_at: '2026-10-01T00:00:01Z',
    data_quality: [],
    sections: {
      fundamentals: {
        status: 'unavailable',
        snapshot: { status: 'unavailable', values: null },
        periods: [],
      },
      technicals: {
        status: 'available',
        message: null,
        interval: '1d',
        source: 'yfinance',
        latest_bar: '2026-10-01T00:00:00Z',
        bars_available: 250,
        indicators: {
          rsi: {
            value: '51.2',
            is_synthetic: false,
            bars_required: 15,
            bars_used: 250,
            field_path: 'market_bars[interval=1d].indicators.rsi',
          },
        },
      },
      financials_trend: { status: 'unavailable', periods: [] },
      news: {
        status: 'unavailable',
        message: 'Add an authorized issuer feed.',
        items: [],
        sources: [],
      },
      data_quality: {
        status: 'issues',
        items: [
          {
            kind: 'unavailable',
            field_path: 'instrument_fundamentals.snapshot.pe',
            reason: 'The provider did not return this field.',
          },
          {
            kind: 'unavailable',
            field_path: 'instrument_fundamentals.snapshot.pb',
            reason: 'The provider did not return this field.',
          },
        ],
      },
      disclaimer: 'Decision support, not investment advice.',
    },
  };

  const markup = renderToStaticMarkup(
    React.createElement(ResearchBriefView, { brief }),
  );
  const visibleMarkup = markup.replace(
    /<details class="research-provenance">[\s\S]*?<\/details>/,
    '',
  );

  assert.match(markup, /research-status-partial/);
  assert.match(markup, /Technicals<\/small><strong>available/);
  assert.match(markup, /The site does not permit this URL to be fetched/);
  assert.match(markup, /Repeated across 2 fields/);
  assert.match(markup, /Technical provenance/);
  assert.doesNotMatch(visibleMarkup, /instrument_fundamentals\.snapshot\.pe/);
  assert.match(markup, /instrument_fundamentals\.snapshot\.pe/);
});
