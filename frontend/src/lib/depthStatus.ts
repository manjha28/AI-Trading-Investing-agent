/** Presentation of the Dhan market-depth connection state. Pure so every state is testable. */
export type DepthStatus =
  | 'not_configured' | 'connecting' | 'subscribed' | 'active' | 'stale' | 'reconnecting'
  | 'no_data' | 'unsupported' | 'unavailable' | 'failed' | 'market_closed';

export type DepthStatusView = { label: string; tone: 'idle' | 'progress' | 'live' | 'warn' | 'bad'; text: string };

export function depthStatusView(status: DepthStatus, message: string, lastSnapshot: string | null): DepthStatusView {
  const when = lastSnapshot ? new Date(lastSnapshot).toLocaleString() : null;
  switch (status) {
    case 'not_configured':
      return { label: 'Not subscribed', tone: 'idle', text: 'Select Dhan as the market-data source and open this tab to subscribe.' };
    case 'connecting':
      return { label: 'Connecting', tone: 'progress', text: message || 'Connecting to the Dhan market-depth feed…' };
    case 'subscribed':
      return { label: 'Subscribed', tone: 'progress', text: message || 'Subscribed to Dhan 20-level depth. Waiting for the first order-book update; nothing is live yet.' };
    case 'active':
      return { label: 'Live', tone: 'live', text: message || 'Receiving live 20-level depth from Dhan.' };
    case 'stale':
      return { label: 'Stale', tone: 'warn', text: message || `No recent depth update. Showing the last received levels${when ? ` (${when})` : ''}; they are not live.` };
    case 'reconnecting':
      return { label: 'Reconnecting', tone: 'warn', text: message || 'The Dhan depth connection dropped. Reconnecting; the levels shown are not live.' };
    case 'no_data':
      return { label: 'No data', tone: 'warn', text: message || 'Dhan has sent no depth for this instrument.' };
    case 'unsupported':
      return { label: 'Unsupported', tone: 'bad', text: message || 'Dhan 20-level depth is not available for this instrument.' };
    case 'market_closed':
      return { label: 'Market closed', tone: 'idle', text: `Market closed. Last known snapshot: ${when ?? 'not available'}.` };
    case 'failed':
    case 'unavailable':
    default:
      return { label: 'Failed', tone: 'bad', text: message || 'Dhan market depth is unavailable.' };
  }
}
