// Automated layout check for the daily report cards (test data harness). Exit 1 on any failure.
// Usage (from frontend/, vite dev server on :5174):  CHROME=<chrome-headless-shell> node e2e/layout/check.mjs [query]
import { chromium } from 'playwright';
const BASE = process.env.BASE ?? 'http://localhost:5174/e2e/layout/harness.html';
const QUERY = process.argv[2] ?? '';
const browser = await chromium.launch({ executablePath: process.env.CHROME ?? undefined });
let failed = 0;
const check = (ok, msg) => { if (!ok) { failed += 1; console.error('FAIL', msg); } else console.log('ok  ', msg); };
for (const w of [1366, 1600, 1920]) {
  const page = await browser.newPage({ viewport: { width: w, height: 1000 } });
  await page.goto(`${BASE}${QUERY}`);
  await page.waitForSelector('.rt-buy');
  const measure = () => page.evaluate(() => {
    const cards = [...document.querySelectorAll('.rt-buy')];
    const rows = new Map();
    for (const c of cards) { const t = Math.round(c.getBoundingClientRect().top); (rows.get(t) ?? rows.set(t, []).get(t)).push(c); }
    const spread = Math.max(0, ...[...rows.values()].map((r) => { const h = r.map((c) => c.getBoundingClientRect().height); return Math.max(...h) - Math.min(...h); }));
    const overflow = cards.flatMap((c) => [...c.querySelectorAll('*')].filter((e) => { const r = e.getBoundingClientRect(), p = c.getBoundingClientRect(); return r.width > 0 && (r.right > p.right + 1 || r.left < p.left - 1); })).length;
    // sections line up: the ladder slot of every card in a row starts at the same y offset from the card top
    const offsets = cards.map((c) => Math.round(c.querySelector('.rt-slot-ladder').getBoundingClientRect().top - c.getBoundingClientRect().top));
    return { n: cards.length, spread, overflow, hscroll: document.documentElement.scrollWidth > document.documentElement.clientWidth, offsetsEqual: new Set(offsets).size === 1 };
  });
  let m = await measure();
  check(m.n >= 6 && m.spread <= 1, `${w}px: cards in a row are the same height (spread ${m.spread.toFixed(2)}px, ${m.n} cards)`);
  check(m.offsetsEqual, `${w}px: the ladder starts at the same height on every card`);
  check(m.overflow === 0, `${w}px: nothing overflows a card (${m.overflow})`);
  check(!m.hscroll, `${w}px: no page horizontal scroll`);
  await page.locator('.rt-buy .rt-why summary').first().click();
  m = await measure();
  check(m.spread <= 1, `${w}px: opening a Why fold does not stretch its row (spread ${m.spread.toFixed(2)}px)`);
  await page.close();
}
await browser.close();
process.exit(failed ? 1 : 0);
