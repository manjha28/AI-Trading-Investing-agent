import { Button, Chip } from './ui/kit';

export type AttentionItem = { id: string; tone: 'negative' | 'caution' | 'info'; text: string; action?: { label: string; onClick: () => void } };

/** The first thing Overview answers: what needs you now. Nothing listed means nothing is pending, and it says so. */
export function NeedsAttention({ items, loading }: { items: AttentionItem[]; loading?: boolean }) {
  return (
    <section className="needs-attention" aria-label="Needs attention">
      <h2>Needs attention {!loading && <Chip tone={items.length ? 'caution' : 'positive'}>{items.length ? `${items.length}` : 'all clear'}</Chip>}</h2>
      {loading ? <p className="ui-muted" role="status" aria-busy="true">Checking…</p> : items.length === 0 ? <p className="ui-muted">Nothing needs your attention right now: no new alerts and every priced position is current.</p> : (
        <ul>{items.map((i) => (
          <li key={i.id}><Chip tone={i.tone}>{i.tone === 'negative' ? 'Act' : i.tone === 'caution' ? 'Check' : 'Info'}</Chip><span>{i.text}</span>{i.action && <Button size="sm" onClick={i.action.onClick}>{i.action.label}</Button>}</li>
        ))}</ul>
      )}
    </section>
  );
}
