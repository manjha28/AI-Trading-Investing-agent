import { useEffect, useState } from 'react';
import { apiRequest } from '../api';

type Item = { symbol: string; instrument_id: string; class: string; rank: number | null; scores: Record<string, number | null>; checklist: string | null; timing: string | null; kind: string | null;
  reasons: string[]; failed_or_unknown: string[]; class_if_unknown_checks_cleared: string | null };
type Resp = { enabled: boolean; message?: string; as_of?: string | null; model_version?: string | null; counts?: Record<string, number>; items: Item[]; disclaimer: string };

const LABEL: Record<string, string> = { multibagger_candidate: 'Multibagger candidate', quality_compounder: 'Quality compounder', watch: 'Watch', avoid: 'Avoid', not_assessable: 'Not assessable' };
const s = (v: number | null | undefined) => (v === null || v === undefined ? '—' : v.toFixed(0));

export function Multibagger({ onOpen }: { onOpen?: (symbol: string) => void }) {
  const [data, setData] = useState<Resp | null>(null);
  const [klass, setKlass] = useState('');
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    setError(null);
    void apiRequest<Resp>(`/research/multibagger${klass ? `?class=${klass}` : ''}`).then(setData).catch(() => setError('The multibagger screen could not be loaded.'));
  }, [klass]);
  if (error) return <p role="alert">{error}</p>;
  if (!data) return <p>Loading the multibagger screen…</p>;
  if (!data.enabled) return <section><h3>Multibagger screen</h3><p>{data.message}</p><p className="muted">{data.disclaimer}</p></section>;
  return (
    <section aria-label="Multibagger screen">
      <h3>Multibagger screen <small>(long-term only; provisional {data.model_version ?? ''}{data.as_of ? `, ${data.as_of}` : ''})</small></h3>
      <div className="rt-tabs" role="group" aria-label="Class filter">
        <button type="button" className={klass === '' ? 'active' : ''} onClick={() => setKlass('')}>All</button>
        {Object.keys(LABEL).map((k) => <button key={k} type="button" className={klass === k ? 'active' : ''} onClick={() => setKlass(k)}>{LABEL[k]} {data.counts?.[k] ?? 0}</button>)}
      </div>
      {data.items.length === 0 ? <p>No stocks in this class.</p> : (
        <table>
          <thead><tr><th>Stock</th><th>Class</th><th>Rank</th><th>Q</th><th>G</th><th>V</th><th>T</th><th>X</th><th>Checklist</th><th>Timing</th><th>Top reasons</th><th>Failed or unknown</th></tr></thead>
          <tbody>
            {data.items.map((i) => (
              <tr key={i.instrument_id}>
                <td>{onOpen ? <button type="button" onClick={() => onOpen(i.symbol)}>{i.symbol}</button> : i.symbol}</td>
                <td>{LABEL[i.class] ?? i.class}{i.class === 'watch' && i.class_if_unknown_checks_cleared && i.class_if_unknown_checks_cleared !== 'watch' ? <small> (could reach {LABEL[i.class_if_unknown_checks_cleared]} once the unknown checks are verified)</small> : null}</td>
                <td>{i.rank ?? '—'}</td><td>{s(i.scores.Q)}</td><td>{s(i.scores.G)}</td><td>{s(i.scores.V)}</td><td>{s(i.scores.T)}</td><td>{s(i.scores.X)}</td>
                <td>{i.checklist ?? '—'}</td><td>{i.timing === 'buy_zone' ? 'Buy zone' : i.timing === 'wait_for_chart' ? 'Wait for chart' : '—'}</td>
                <td><ul>{i.reasons.map((r) => <li key={r}>{r}</li>)}</ul></td>
                <td><ul>{i.failed_or_unknown.slice(0, 4).map((r) => <li key={r}>{r}</li>)}</ul></td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <p className="muted">{data.disclaimer} A score exists only when 70% of its inputs are stored; unknown is shown as — and never as zero.</p>
    </section>
  );
}
