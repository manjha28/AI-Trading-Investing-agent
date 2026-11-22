export type BrokerLike = { slug: string; name: string; enabled: boolean; synthetic: boolean; connected: boolean; connection_status: 'connected' | 'expired' | 'not_connected'; integration_status: 'available' | 'setup_required' | 'pending' | 'synthetic' };

/** The mock broker is for development only: shown when VITE_SHOW_MOCK_BROKER=1 or localStorage show_mock_broker=1. */
export const SHOW_MOCK: boolean = (import.meta.env?.VITE_SHOW_MOCK_BROKER === '1') || (typeof localStorage !== 'undefined' && localStorage.getItem('show_mock_broker') === '1');

/** The connected broker first (Dhan before the rest), then brokers that are usable; unconfigured, expired and not-connected ones go under "Other brokers"; the mock broker is hidden unless the dev setting is on.
 *  Angel One has its own market-data card: it is a primary card only while its session is usable. */
export function groupBrokers<T extends BrokerLike>(brokers: T[], opts: { angelOneUsable: boolean; showMock?: boolean }): { primary: (T | 'angel_one')[]; others: (T | 'angel_one')[] } {
  const visible = brokers.filter((b) => b.slug !== 'angel_one' && (opts.showMock || !(b.synthetic || b.slug === 'mock')));
  const rank = (b: T) => (b.connected ? 0 : 1) + (b.slug === 'dhan' ? -0.5 : 0);
  const sorted = [...visible].sort((a, b) => rank(a) - rank(b) || a.name.localeCompare(b.name));
  const primary: (T | 'angel_one')[] = [];
  const others: (T | 'angel_one')[] = [];
  for (const b of sorted) {
    const usable = b.connected || (b.slug === 'dhan' && b.enabled && b.integration_status === 'available');       // Dhan stays visible so it can be connected
    (usable ? primary : others).push(b);
  }
  (opts.angelOneUsable ? primary : others).push('angel_one');
  return { primary, others };
}
