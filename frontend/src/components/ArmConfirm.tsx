import { createPortal } from 'react-dom';
import { Button, Sheet } from './ui/kit';

/** The explicit confirmation step before arming: it names the venue, the strategies, the quantity cap and the leverage, so nothing is armed by a stray click. */
export function ArmConfirm({ open, venue, strategies, quantityCap, leverage, tradingWindow, onConfirm, onCancel, busy }: { open: boolean; venue: string; strategies: string[]; quantityCap: string; leverage?: string; tradingWindow: string; onConfirm: () => void; onCancel: () => void; busy?: boolean }) {
  // Drawn on the page body: inside the blurred top bar a fixed dialog is trapped in the bar and cannot be seen.
  return createPortal(
    <Sheet open={open} onClose={onCancel} title={`Turn live orders on for ${venue}?`}>
      <p><strong>Warning: this lets the app send real orders with real money on your own {venue} account.</strong> Losses are possible.</p>
      <ul>
        <li>Venue: <strong>{venue}</strong></li>
        <li>Strategies: <strong>{strategies.join(', ') || 'none'}</strong></li>
        <li>Quantity cap: <strong>{quantityCap}</strong></li>
        {leverage && <li>Leverage: <strong>{leverage}</strong></li>}
        <li>Trading window: <strong>{tradingWindow}</strong></li>
      </ul>
      <p className="ui-muted">Nothing is sent until a strategy you switch on signals inside the window. The kill switch disarms instantly. Decision support, not investment advice.</p>
      <div className="ui-row"><Button variant="danger" loading={busy} onClick={onConfirm}>Yes, turn on live orders</Button><Button variant="ghost" onClick={onCancel}>Cancel</Button></div>
    </Sheet>,
    document.body,
  );
}
