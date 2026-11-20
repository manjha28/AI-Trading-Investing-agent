import { useCallback, useEffect, useMemo, useState } from 'react';
import { fmtMoney, lab, messageOf } from './lab';
import { Notice, Pill } from './shared';
import type { StrategyRow } from './types';

type Param = { name: string; default: number; min: number; max: number; description: string };
type Evidence = {
  data_window?: { start?: string; end?: string }; symbols?: string[]; bars?: number; timeframe?: string; cost_profile?: string; slippage_bps?: number | string;
  trades?: number; win_rate?: number; gross_pnl_before_costs?: string; total_charges?: string; total_slippage?: string; net_profit?: string; profit_factor_net?: number | string;
  max_drawdown_pct?: number; symbols_profitable?: number; symbols_tested?: number; out_of_sample?: { trades?: number; net_profit?: string; profit_factor_net?: number | string } | null; verdict?: string; note?: string;
};
export type RegistryStrategy = { key: string; name: string; family: string; summary: string; rationale: string; version: string; evidence: Evidence | null; params: Param[]; installed_strategy_id: string | null };

const FAMILY_LABEL: Record<string, string> = { trend: 'Trend', momentum: 'Momentum', mean_reversion: 'Mean reversion', breakout: 'Breakout', volatility: 'Volatility', volume: 'Volume' };
const tone = (v: unknown) => { const n = Number(v); return !Number.isFinite(n) || n === 0 ? '' : n > 0 ? 'sl-pos' : 'sl-neg'; };

export function StrategyRegistry({ strategies, onInstalled, onOpen, goLab }: {
  strategies: StrategyRow[]; onInstalled: () => void; onOpen: (s: StrategyRow) => void; goLab: () => void;
}) {
  const [items, setItems] = useState<RegistryStrategy[] | null>(null);
  const [family, setFamily] = useState('all');
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => lab<{ strategies: RegistryStrategy[] }>('/registry').then((r) => setItems(r.strategies)).catch((e) => setError(messageOf(e))), []);
  useEffect(() => { void load(); }, [load]);

  const families = useMemo(() => ['all', ...Array.from(new Set((items ?? []).map((i) => i.family)))], [items]);
  const shown = (items ?? []).filter((i) => family === 'all' || i.family === family);
  const missing = (items ?? []).filter((i) => !i.installed_strategy_id).length;

  async function install() {
    setBusy(true); setError(null); setNotice(null);
    try {
      const r = await lab<{ installed: number; skipped: { name: string; reason: string }[] }>('/starters/install', { method: 'POST' });
      setNotice(r.installed ? `Installed ${r.installed} strategy(ies) into your library (suspended, no symbols attached). Open one in the Builder or run it in the Backtest Lab.` : 'Everything is already installed.');
      await load();
      onInstalled();
    } catch (e) { setError(messageOf(e)); } finally { setBusy(false); }
  }

  return (
    <div className="sl-stack">
      <Notice tone="info">Registry strategies are defined in code, built from the same indicators and rules as anything you build here, and are fully editable once installed. Recorded results are <strong>hypothetical backtests</strong> after charges and slippage — never real P&amp;L, a position, or an order.</Notice>
      {error && <Notice tone="bad">{error}</Notice>}
      {notice && <Notice tone="ok">{notice}</Notice>}
      <div className="sl-toolbar">
        <label>Family <select value={family} onChange={(e) => setFamily(e.target.value)} aria-label="Family">{families.map((f) => <option key={f} value={f}>{f === 'all' ? 'All families' : FAMILY_LABEL[f] ?? f}</option>)}</select></label>
        <button type="button" className="sl-primary" disabled={busy || missing === 0} onClick={() => void install()}>{busy ? 'Installing…' : missing ? `Install ${missing} into my library` : 'All installed'}</button>
      </div>
      {items === null && !error && <p className="sl-muted">Loading registry…</p>}
      {items !== null && items.length === 0 && <p className="sl-muted">No registry strategies are shipped yet.</p>}
      <div className="reg-grid">
        {shown.map((s) => {
          const owned = s.installed_strategy_id ? strategies.find((x) => x.id === s.installed_strategy_id) : undefined;
          const e = s.evidence;
          return (
            <article key={s.key} className="sl-card reg-card" aria-label={s.name}>
              <div className="sl-card-head"><h3>{s.name}</h3><span><Pill tone="info">{FAMILY_LABEL[s.family] ?? s.family}</Pill> {s.installed_strategy_id ? <Pill tone="ok">installed</Pill> : null}</span></div>
              <p>{s.summary}</p>
              <details><summary>Why it might work (and when it won&apos;t)</summary><p className="sl-muted">{s.rationale}</p></details>
              <details><summary>{s.params.length} parameters</summary>
                <table className="sl-table"><thead><tr><th>Name</th><th className="r">Default</th><th className="r">Range</th><th>Meaning</th></tr></thead>
                  <tbody>{s.params.map((p) => <tr key={p.name}><td><code>{p.name}</code></td><td className="r">{p.default}</td><td className="r">{p.min}–{p.max}</td><td>{p.description}</td></tr>)}</tbody></table>
              </details>
              {e ? (
                <div className="reg-evidence" aria-label={`${s.name} recorded result`}>
                  <p className="sl-muted"><strong>Recorded result (hypothetical)</strong> · {e.symbols?.length ?? '—'} symbols · {e.timeframe ?? '—'} · {e.data_window?.start?.slice(0, 10) ?? '—'} → {e.data_window?.end?.slice(0, 10) ?? '—'} · {e.bars?.toLocaleString('en-IN') ?? '—'} 1-minute bars · {e.cost_profile ?? 'charges applied'}</p>
                  <dl className="reg-metrics">
                    <div><dt>Trades</dt><dd>{e.trades ?? '—'}</dd></div>
                    <div><dt>Gross</dt><dd>{fmtMoney(e.gross_pnl_before_costs)}</dd></div>
                    <div><dt>Charges</dt><dd>{fmtMoney(e.total_charges)}</dd></div>
                    <div><dt>Slippage</dt><dd>{fmtMoney(e.total_slippage)}</dd></div>
                    <div><dt>Net</dt><dd className={tone(e.net_profit)}>{fmtMoney(e.net_profit)}</dd></div>
                    <div><dt>Profit factor</dt><dd>{e.profit_factor_net != null ? Number(e.profit_factor_net).toFixed(2) : '—'}</dd></div>
                    <div><dt>Symbols +</dt><dd>{e.symbols_tested ? `${e.symbols_profitable}/${e.symbols_tested}` : '—'}</dd></div>
                    {e.out_of_sample && <div><dt>Out-of-sample net</dt><dd className={tone(e.out_of_sample.net_profit)}>{fmtMoney(e.out_of_sample.net_profit)}</dd></div>}
                  </dl>
                  {e.verdict && <p><strong>{e.verdict}</strong></p>}
                  {e.note && <p className="sl-muted">{e.note}</p>}
                </div>
              ) : <p className="sl-muted">Not tested: no result is recorded for this strategy.</p>}
              <div className="sl-toolbar">
                {owned ? <><button type="button" onClick={() => onOpen(owned)}>Open in Builder</button><button type="button" onClick={goLab}>Run in Backtest Lab</button></> : <span className="sl-muted">Install to edit or backtest.</span>}
              </div>
            </article>
          );
        })}
      </div>
    </div>
  );
}
