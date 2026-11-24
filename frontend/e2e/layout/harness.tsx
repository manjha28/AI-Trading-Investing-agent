import React from 'react';
import { createRoot } from 'react-dom/client';
import '../../src/styles/tokens.css';
import '../../src/workspace.css';
import '../../src/styles/theme.css';
import '../../src/components/ui/ui.css';
import '../../src/pages/research.css';
import { DailyReport } from '../../src/pages/DailyReport';
import { report, buyRows } from './fixtures';

// Test harness: answers the report endpoints with the hand-written fixtures. Nothing here ships in the app.
window.fetch = ((input: RequestInfo | URL) => {
  const url = String(input);
  const one = buyRows.find((r) => url.endsWith(`/daily-report/${r.instrument_id}`));
  const body = one ?? report;
  return Promise.resolve(new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } }));
}) as typeof window.fetch;
const flags = new URLSearchParams(window.location.search);
createRoot(document.getElementById('root')!).render(<div className="rt-root research-pipeline-page" data-v2={flags.get('v2') ?? ''}><DailyReport onOpen={() => undefined} /></div>);
