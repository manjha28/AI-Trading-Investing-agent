// Screenshots + layout measurements of the daily report harness. Usage (from frontend/, dev server on :5174):  OUT=../diagnostics/ui/overnight-2026-11-25/before node e2e/layout/shoot.mjs [query]
import { mkdirSync, writeFileSync } from 'node:fs';
import { chromium } from 'playwright';
const BASE = process.env.BASE ?? 'http://localhost:5174/e2e/layout/harness.html';
const OUT = process.env.OUT ?? '../diagnostics/ui/overnight-2026-11-25/before';
const QUERY = process.argv[2] ?? '';
mkdirSync(OUT, { recursive: true });
const browser = await chromium.launch({ executablePath: process.env.CHROME ?? undefined });
const result = {};
for (const w of [1366, 1600, 1920]) {
  const page = await browser.newPage({ viewport: { width: w, height: 1000 } });
  await page.goto(`${BASE}${QUERY}`);
  await page.waitForSelector('.rt-buy', { timeout: 15000 });
  await page.waitForTimeout(500);
  result[w] = await page.evaluate(() => {
    const cards = [...document.querySelectorAll('.rt-buy')];
    const rows = new Map();
    for (const c of cards) { const top = Math.round(c.getBoundingClientRect().top); (rows.get(top) ?? rows.set(top, []).get(top)).push(c); }
    const rowSpread = [...rows.values()].map((r) => { const hs = r.map((c) => c.getBoundingClientRect().height); return Math.max(...hs) - Math.min(...hs); });
    const overflow = cards.flatMap((c) => [...c.querySelectorAll('*')].filter((e) => { const r = e.getBoundingClientRect(), p = c.getBoundingClientRect(); return r.width > 0 && (r.right > p.right + 1 || r.left < p.left - 1); }).map((e) => e.className || e.tagName));
    return { cards: cards.length, heights: cards.map((c) => Math.round(c.getBoundingClientRect().height)), maxRowSpread: Math.max(0, ...rowSpread), overflowing: overflow.length, hscroll: document.documentElement.scrollWidth > document.documentElement.clientWidth };
  });
  await page.screenshot({ path: `${OUT}/daily-report-${w}.png`, fullPage: true });
  await page.close();
}
writeFileSync(`${OUT}/measure.json`, JSON.stringify(result, null, 1));
console.log(JSON.stringify(result));
await browser.close();
