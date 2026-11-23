// Phase 0 baseline: screenshots of every page at 1440/1024/390 px, dark and light, plus a horizontal-overflow record per page.
// Usage (from frontend/, app already running):
//   E2E_BASE=https://finbyte-nexus-web.fly.dev E2E_EMAIL=... E2E_PASSWORD=... E2E_TOTP=123456 OUT=../diagnostics/ui/before node e2e/visual/shots.mjs
// Needs `playwright` (npm i -D playwright) and a Chromium build. Credentials come from the environment only; nothing is stored.
import { mkdirSync, writeFileSync } from 'node:fs';
import { chromium } from 'playwright';

const BASE = process.env.E2E_BASE ?? 'http://localhost:5173';
const OUT = process.env.OUT ?? '../diagnostics/ui/before';
const SIZES = [[1440, 900], [1024, 768], [390, 844]];
const THEMES = ['dark', 'light'];
// Navigation is state based (no URLs), so each page is reached by clicking its nav item, then optional steps.
const PAGES = [
  { name: 'overview', nav: 'Overview' },
  { name: 'market-watch', nav: 'Market watch' },
  { name: 'market-watch-stock', nav: 'Market watch', steps: async (p) => { await p.locator('table tbody tr').first().click({ timeout: 5000 }); } },
  { name: 'research-daily-report', nav: 'Research', tab: 'Daily report' },
  { name: 'research-stock', nav: 'Research', tab: 'Stock research' },
  { name: 'research-multibagger', nav: 'Research', tab: 'Multibagger screen' },
  { name: 'signals-dashboard', nav: 'Signals', tab: 'Dashboard' },
  { name: 'signals-strategies', nav: 'Signals', tab: 'Strategies' },
  { name: 'signals-backtest', nav: 'Signals', tab: 'Backtest' },
  { name: 'signals-forward-test', nav: 'Signals', tab: 'Forward test' },
  { name: 'signals-live', nav: 'Signals', tab: 'Live signals' },
  { name: 'signals-jobs', nav: 'Signals', tab: 'Jobs' },
  { name: 'crypto-markets', nav: 'Crypto', tab: 'Markets & charts' },
  { name: 'crypto-lab', nav: 'Crypto', tab: 'Strategy lab' },
  { name: 'crypto-forward', nav: 'Crypto', tab: 'Forward test' },
  { name: 'crypto-feed', nav: 'Crypto', tab: 'Live feed' },
  { name: 'crypto-execution', nav: 'Crypto', tab: 'Execution' },
  { name: 'dhan-connection', nav: 'Brokers' },
];

async function click(page, name) {
  const target = page.getByRole('button', { name, exact: false }).or(page.getByRole('tab', { name })).or(page.getByRole('link', { name })).first();
  await target.click({ timeout: 8000 });
}

async function login(page) {
  await page.goto(BASE);
  await page.getByLabel(/email/i).fill(process.env.E2E_EMAIL ?? '');
  await page.getByLabel(/password/i).first().fill(process.env.E2E_PASSWORD ?? '');
  await page.getByRole('button', { name: /sign in|log in/i }).click();
  if (process.env.E2E_TOTP) {
    await page.getByLabel(/code|authenticator/i).first().fill(process.env.E2E_TOTP).catch(() => {});
    await page.getByRole('button', { name: /verify/i }).click().catch(() => {});
  }
  await page.waitForLoadState('networkidle');
}

const browser = await chromium.launch();
const overflow = [];
mkdirSync(OUT, { recursive: true });
// the signed-out Auth page is captured before logging in
for (const [w, h] of SIZES) for (const theme of THEMES) {
  const ctx = await browser.newContext({ viewport: { width: w, height: h }, colorScheme: theme });
  const page = await ctx.newPage();
  await page.goto(BASE); await page.evaluate((t) => document.documentElement.setAttribute('data-theme', t), theme);
  await page.screenshot({ path: `${OUT}/auth-${w}-${theme}.png`, fullPage: true });
  overflow.push({ page: 'auth', width: w, theme, overflow: await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth) });
  await ctx.close();
}
for (const [w, h] of SIZES) for (const theme of THEMES) {
  const ctx = await browser.newContext({ viewport: { width: w, height: h }, colorScheme: theme });
  const page = await ctx.newPage();
  const errors = [];
  page.on('console', (m) => m.type() === 'error' && errors.push(m.text()));
  await login(page);
  await page.evaluate((t) => document.documentElement.setAttribute('data-theme', t), theme);
  for (const def of PAGES) {
    try {
      await click(page, def.nav);
      if (def.tab) await click(page, def.tab);
      if (def.steps) await def.steps(page);
      await page.waitForLoadState('networkidle', { timeout: 15000 }).catch(() => {});
      await page.waitForTimeout(600);
      await page.screenshot({ path: `${OUT}/${def.name}-${w}-${theme}.png`, fullPage: true });
      overflow.push({ page: def.name, width: w, theme, overflow: await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth) });
    } catch (e) {
      overflow.push({ page: def.name, width: w, theme, overflow: null, error: String(e.message).split('\n')[0] });
    }
  }
  overflow.push({ page: '_console_errors', width: w, theme, errors });
  await ctx.close();
}
await browser.close();
writeFileSync(`${OUT}/overflow.json`, JSON.stringify(overflow, null, 1));
const bad = overflow.filter((r) => typeof r.overflow === 'number' && r.overflow > 0);
console.log(`${overflow.length} records; ${bad.length} pages overflow horizontally`);
for (const r of bad) console.log(`  ${r.page} @${r.width} ${r.theme}: +${r.overflow}px`);
