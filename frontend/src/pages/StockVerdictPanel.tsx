import { useEffect, useState } from 'react';
import { apiRequest } from '../api';
import { formatIST } from '../lib/formatIST';
import { tickPrice } from '../lib/formatPrice';

type Lens = { score: number | null; direction: string; facts: string[]; coverage: number; regime?: string };
type Levels = { entry_status?: string; entry_zone?: { low: string; high: string } | null; stop?: string; targets?: { label: string; price: string; risk_reward?: number }[]; entry_label?: string } | null;
type Horizon = { verdict: string; evidence_score: number | null; evidence?: { label: string } | null; confidence?: { line: string | null; band: string | null; validated: boolean } | null; reasons: string[]; veto: string | null;
  size: number | null; levels: Levels; grade: string | null; lenses: Record<string, Lens> };
type Verdict = { enabled: boolean; shadow?: boolean; message?: string; symbol?: string; as_of?: string | null; headline?: { horizon: string; verdict: string; evidence: number } | null; horizons: Record<string, Horizon>;
  multibagger?: { class: string; checklist: string | null; timing: string | null; failed_or_unknown: string[] } | null; regime?: { regime: string | null; facts: string[] } | null; risks_and_events?: string[]; disclaimer: string };

const HZ: Record<string, string> = { swing: 'Swing', positional: 'Positional', long_term: 'Long-term' };
const VERDICT: Record<string, string> = { buy_now: 'Buy now', buy_on_trigger: 'Buy on trigger', watch: 'Watch', not_now: 'Not now', avoid: 'Avoid' };
const LENS: Record<string, string> = { chart: 'Chart', financial_trends: 'Financial trends', fundamentals: 'Fundamentals', growth: 'Growth', valuation: 'Valuation', news_events: 'News and events', market_regime: 'Market regime' };

export function StockVerdictPanel({ symbol }: { symbol: string }) {
  const [data, setData] = useState<Verdict | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let active = true;
    setData(null);
    setError(null);
    void apiRequest<Verdict>(`/research/stock/${encodeURIComponent(symbol)}/verdict`).then((d) => { if (active) setData(d); }).catch((e: Error) => { if (active) setError(e.message || 'The verdict could not be loaded.'); });
    return () => { active = false; };
  }, [symbol]);
  if (error) return <p role="alert">{error}</p>;
  if (!data || !data.enabled || !data.as_of) return data && !data.enabled ? null : data?.message ? <p className="rt-muted">{data.message}</p> : null;
  const horizons = Object.keys(HZ).filter((h) => data.horizons[h]);
  const first = data.horizons[horizons[0]];
  return (
    <section className="rt-card stock-verdict" aria-label="Stock verdict">
      <header>
        <h3>Verdict {data.headline ? <span className={`rt-chip rt-chip-${data.headline.verdict}`}>{VERDICT[data.headline.verdict]} · {HZ[data.headline.horizon]}</span> : null}</h3>
        {data.shadow && <small className="rt-note" role="status">Shadow mode: nightly results for you to read; the Daily report does not use them yet (stock_verdict_enabled is off).</small>}
        <small className="rt-muted">As of {formatIST(data.as_of)} · stock-verdict (provisional)</small>
      </header>
      <table className="rt-table">
        <thead><tr><th>Horizon</th><th>Verdict</th><th>Confidence</th><th>Entry</th><th>Stop</th><th>T1 / R:R</th><th>Size</th><th>Top reasons</th></tr></thead>
        <tbody>
          {horizons.map((h) => {
            const d = data.horizons[h];
            const t1 = d.levels?.targets?.[0];
            return (
              <tr key={h}>
                <td>{HZ[h]}</td>
                <td><b>{VERDICT[d.verdict] ?? d.verdict}</b>{d.veto ? <small> (veto: {d.veto.replace('_', ' ')})</small> : null}</td>
                <td>{d.confidence?.line ?? d.evidence?.label ?? (d.evidence_score !== null ? `Evidence score ${Math.round(d.evidence_score)} (not yet validated)` : '—')}</td>
                <td>{d.levels?.entry_zone ? `${tickPrice(d.levels.entry_zone.low)} – ${tickPrice(d.levels.entry_zone.high)}` : '—'}</td>
                <td>{d.levels?.stop ? tickPrice(d.levels.stop) : '—'}</td>
                <td>{t1 ? `${tickPrice(t1.price)}${t1.risk_reward ? ` · ${t1.risk_reward}R` : ''}` : '—'}</td>
                <td>{d.size ? `${Math.round(d.size * 100)}%` : '—'}</td>
                <td><ul>{d.reasons.slice(0, 3).map((r) => <li key={r}>{r}</li>)}</ul></td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <div className="rt-lens-grid">
        {Object.keys(LENS).map((k) => {
          const l = first?.lenses?.[k];
          if (!l) return null;
          return (
            <article key={k} className="rt-lens">
              <h4>{LENS[k]} <small>{l.score === null ? 'no score' : Math.round(l.score)} · {l.direction}</small></h4>
              <ul>{(l.facts ?? []).slice(0, 3).map((f) => <li key={f}>{f}</li>)}</ul>
            </article>
          );
        })}
      </div>
      {data.multibagger && <p className="rt-note"><b>Multibagger class:</b> {data.multibagger.class.replace(/_/g, ' ')} · {data.multibagger.checklist ?? '—'}{data.multibagger.failed_or_unknown.length ? ` · open: ${data.multibagger.failed_or_unknown.slice(0, 3).join('; ')}` : ''}</p>}
      {data.regime?.regime && <p className="rt-note"><b>Market regime:</b> {data.regime.regime}. {data.regime.facts?.[0]}</p>}
      {!!data.risks_and_events?.length && <ul className="rt-note">{data.risks_and_events.map((r) => <li key={r}>{r}</li>)}</ul>}
      <p className="rt-note">{data.disclaimer}</p>
    </section>
  );
}
