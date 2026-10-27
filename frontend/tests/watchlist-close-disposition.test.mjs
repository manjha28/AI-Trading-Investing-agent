import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { fileURLToPath } from 'node:url';
import react from '@vitejs/plugin-react';
import { createServer } from 'vite';

let watchlistCloseDisposition;
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
  ({ watchlistCloseDisposition } = await vite.ssrLoadModule(
    '/src/pages/watchlistCloseDisposition.ts',
  ));
});

after(async () => {
  await vite?.close();
});

test('clean close after market_closed status keeps market-closed UI state', () => {
  assert.equal(
    watchlistCloseDisposition(1000, '', true, false),
    'market_closed',
  );
});

test('market-closed close reason recovers when status was not received', () => {
  assert.equal(
    watchlistCloseDisposition(1000, 'Market closed', false, false),
    'market_closed',
  );
});
