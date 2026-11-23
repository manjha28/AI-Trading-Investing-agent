import { Button, Sheet } from './ui/kit';

/** The explicit confirmation step before arming: it names the venue, the strategies, the quantity cap and the leverage, so nothing is armed by a stray click. */
export function ArmConfirm({ open, venue, strategies, quantityCap, leverage, tradingWindow, onConfirm, onCancel, busy }: { open: boolean; venue: string; strategies: string[]; quantityCap: string; leverage?: string; tradingWindow: string; onConfirm: () => void; onCancel: () => void; busy?: boolean }) {
  return (
    <Sheet open={open} onClose={onCancel} title={`Arm live orders on ${venue}?`}>
      <p>You are about to allow <strong>real orders on your own {venue} account</strong>.</p>
      <ul>
        <li>Venue: <strong>{venue}</strong></li>
        <li>Strategies: <strong>{strategies.join(', ') || 'none'}</strong></li>
        <li>Quantity cap: <strong>{quantityCap}</strong></li>
        {leverage && <li>Leverage: <strong>{leverage}</strong></li>}
        <li>Trading window: <strong>{tradingWindow}</strong></li>
      </ul>
      <p className="ui-muted">Nothing is sent until a strategy you switch on signals inside the window. The kill switch disarms instantly. Decision support, not investment advice.</p>
      <div className="ui-row"><Button variant="danger" loading={busy} onClick={onConfirm}>Yes, arm {venue}</Button><Button variant="ghost" onClick={onCancel}>Cancel</Button></div>
    </Sheet>
  );
}
