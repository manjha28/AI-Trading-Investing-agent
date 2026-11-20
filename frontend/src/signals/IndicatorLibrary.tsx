import { Fragment, useMemo, useState } from 'react';
import { Scroll } from './shared';
import type { Meta } from './types';

export function IndicatorLibrary({ meta, onUse }: { meta: Meta; onUse: (id: string) => void }) {
  const [q, setQ] = useState('');
  const [cat, setCat] = useState('all');
  const [openId, setOpenId] = useState<string | null>(null);
  const cats = useMemo(() => ['all', ...Array.from(new Set(meta.indicators.map((i) => i.category)))], [meta]);
  const rows = meta.indicators.filter((i) => (cat === 'all' || i.category === cat) && (!q.trim() || `${i.name} ${i.id}`.toLowerCase().includes(q.trim().toLowerCase())));
  return (
    <div>
      <div className="sl-filters" role="search">
        <input aria-label="Search indicators" placeholder="Search indicators…" value={q} onChange={(e) => setQ(e.target.value)} />
        <div className="sl-chips" role="group" aria-label="Category">{cats.map((c) => <button key={c} type="button" className={`sl-chip ${cat === c ? 'active' : ''}`} aria-pressed={cat === c} onClick={() => setCat(c)}>{c.replace('_', ' ')}</button>)}</div>
        <span className="sl-muted">{rows.length} of {meta.indicators.length} · library v{meta.indicator_library_version}</span>
      </div>
      <p className="sl-muted">Every indicator is computed from completed candles only, returns nothing (not an estimate) during warm-up, and is the same code used by live signals and backtests.</p>
      <Scroll label="Indicators">
        <table className="sl-table">
          <thead><tr><th>Indicator</th><th>Category</th><th>Outputs</th><th>Parameters (default)</th><th>Warm-up</th><th /></tr></thead>
          <tbody>
            {rows.map((i) => (
              <Fragment key={i.id}>
                <tr>
                  <td><button type="button" className="sl-link" aria-expanded={openId === i.id} onClick={() => setOpenId(openId === i.id ? null : i.id)}>{i.name}</button> <code>{i.id}</code></td>
                  <td>{i.category.replace('_', ' ')}</td><td title={i.outputs.join(', ')}>{i.outputs.length > 8 ? `${i.outputs.slice(0, 8).join(', ')} … +${i.outputs.length - 8} more` : i.outputs.join(', ')}</td>
                  <td>{Object.entries(i.params).map(([k, p]) => `${k}=${p.default}`).join(', ') || '—'}</td><td className="sl-r">{i.minimum_bars_default} bars</td>
                  <td><button type="button" onClick={() => onUse(i.id)}>Use in builder</button></td>
                </tr>
                {openId === i.id && (
                  <tr className="sl-detail"><td colSpan={6}>
                    <p><strong>Formula:</strong> {i.formula}</p>
                    {i.notes && <p><strong>Note:</strong> {i.notes}</p>}
                    <p><strong>Parameter ranges:</strong> {Object.entries(i.params).map(([k, p]) => `${k}: ${p.kind === 'choice' ? p.choices.join('|') : `${p.min ?? '−∞'}…${p.max ?? '∞'}`}`).join(' · ') || 'none'}</p>
                  </td></tr>
                )}
              </Fragment>
            ))}
          </tbody>
        </table>
      </Scroll>
    </div>
  );
}
