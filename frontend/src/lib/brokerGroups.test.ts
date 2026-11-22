import { describe, expect, it } from 'vitest';
import { groupBrokers, type BrokerLike } from './brokerGroups';

const b = (slug: string, extra: Partial<BrokerLike> = {}): BrokerLike => ({ slug, name: slug, enabled: true, synthetic: false, connected: false, connection_status: 'not_connected', integration_status: 'setup_required', ...extra });
const name = (x: BrokerLike | 'angel_one') => (x === 'angel_one' ? 'angel_one' : x.slug);

describe('groupBrokers', () => {
  const brokers = [b('mock', { synthetic: true, integration_status: 'synthetic' }), b('zerodha'), b('upstox'), b('dhan', { connected: true, connection_status: 'connected', integration_status: 'available' })];
  it('puts the connected broker first, hides the mock broker, and collapses unconfigured brokers and an expired Angel One', () => {
    const g = groupBrokers(brokers, { angelOneUsable: false });
    expect(g.primary.map(name)).toEqual(['dhan']);
    expect(g.others.map(name)).toEqual(['upstox', 'zerodha', 'angel_one']);
  });
  it('shows the mock broker only when the dev setting is on, and a usable Angel One as a primary card', () => {
    expect(groupBrokers(brokers, { angelOneUsable: false, showMock: true }).others.map(name)).toContain('mock');
    expect(groupBrokers(brokers, { angelOneUsable: true }).primary.map(name)).toEqual(['dhan', 'angel_one']);
  });
  it('keeps an unconnected but connectable Dhan visible so it can be connected', () => {
    const g = groupBrokers([b('dhan', { integration_status: 'available' }), b('zerodha')], { angelOneUsable: false });
    expect(g.primary.map(name)).toEqual(['dhan']);
  });
});
