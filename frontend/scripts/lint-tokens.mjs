// npm run lint:tokens        fails when a file has MORE raw colours or colour/font/spacing inline styles than the baseline, or a new file has any.
// npm run lint:tokens -- --update   rewrites the baseline after you REMOVE violations (it can only ever shrink; never raise it by hand).
// Allowed everywhere: styles/tokens.css (the one place values live), style.css (unused legacy shell), tests, and chart colour config (charts, lightweight-charts options).
import { readdirSync, readFileSync, writeFileSync, existsSync } from 'node:fs';
import { join, relative } from 'node:path';

const SRC = new URL('../src/', import.meta.url).pathname;
const BASELINE = new URL('./lint-tokens.baseline.json', import.meta.url).pathname;
const ALLOW = [/styles\/tokens\.css$/, /(^|\/)style\.css$/, /\.test\.tsx?$/, /(^|\/)charts?\.tsx$/, /CryptoCharts\.tsx$/, /Chart[A-Za-z]*\.tsx$/];
const RAW_COLOUR = /#[0-9a-fA-F]{3,8}\b|\brgba?\(|\bhsla?\(/g;
const INLINE_STYLE = /style=\{\{([^}]*)\}\}/g;
const STYLE_KEYS = /\b(color|background|backgroundColor|border|borderColor|font|fontSize|fontFamily|fontWeight|margin|padding|gap|width|height)\b\s*:/;

function walk(dir, out = []) {
  for (const e of readdirSync(dir, { withFileTypes: true })) {
    const p = join(dir, e.name);
    if (e.isDirectory()) walk(p, out); else if (/\.(css|tsx)$/.test(e.name)) out.push(p);
  }
  return out;
}

const found = {};
for (const file of walk(SRC)) {
  const rel = relative(SRC, file);
  if (ALLOW.some((re) => re.test(rel))) continue;
  const text = readFileSync(file, 'utf8');
  let n = (text.match(RAW_COLOUR) ?? []).length;
  if (file.endsWith('.tsx')) for (const m of text.matchAll(INLINE_STYLE)) if (STYLE_KEYS.test(m[1])) n += 1;
  if (n > 0) found[rel] = n;
}

if (process.argv.includes('--update')) {
  const old = existsSync(BASELINE) ? JSON.parse(readFileSync(BASELINE, 'utf8')) : null;
  if (old) for (const [f, n] of Object.entries(found)) if (n > (old[f] ?? 0)) { console.error(`refusing to raise the baseline: ${f} ${old[f] ?? 0} -> ${n}`); process.exit(1); }
  writeFileSync(BASELINE, JSON.stringify(found, null, 1) + '\n');
  console.log(`baseline written: ${Object.values(found).reduce((a, b) => a + b, 0)} violations in ${Object.keys(found).length} files`);
  process.exit(0);
}
const base = existsSync(BASELINE) ? JSON.parse(readFileSync(BASELINE, 'utf8')) : {};
const bad = Object.entries(found).filter(([f, n]) => n > (base[f] ?? 0));
if (bad.length) {
  for (const [f, n] of bad) console.error(`lint:tokens: ${f} has ${n} raw colour / inline style value(s), baseline allows ${base[f] ?? 0}. Use a token from styles/tokens.css or a class.`);
  process.exit(1);
}
const total = Object.values(found).reduce((a, b) => a + b, 0);
console.log(`lint:tokens ok: ${total} legacy violations remain in ${Object.keys(found).length} files (baseline ${Object.values(base).reduce((a, b) => a + b, 0)})`);
