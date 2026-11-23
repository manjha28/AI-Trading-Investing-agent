import { describe, expect, it } from 'vitest';
import { footerWording, sidebarWording, todayLine, type ExecutionStates, type VenueState } from './executionState';

const v = (venue: 'dhan' | 'delta', state: 'OFF' | 'ARMED_BUT_BLOCKED' | 'LIVE', reasons: string[], today = { sent_to_broker: 0, rejected_by_broker: 0, blocked_attempts: 0 }): VenueState => ({ venue, label: venue, deployment_flag: { name: 'X', enabled: true }, armed: false, armed_at: null, window: { kind: 'x', text: 'x', open_now: true }, strategies: [], live_tests: 0, effective: { state, reasons }, limits: {}, today });
const states = (dhan: VenueState, delta: VenueState): ExecutionStates => ({ dhan, delta, as_of: 'x' });

describe('execution wording comes from the effective state', () => {
  it('prints each venue state in the sidebar and the first reason in the footer', () => {
    const s = states(v('dhan', 'OFF', ['you have not armed live execution']), v('delta', 'LIVE', []));
    expect(sidebarWording(s)).toBe('Execution · Dhan OFF · Delta LIVE');
    expect(footerWording(s)).toBe('Dhan OFF (you have not armed live execution) · Delta LIVE');
    expect(sidebarWording(null)).toContain('checking'); expect(footerWording(states(v('dhan', 'ARMED_BUT_BLOCKED', ['outside the entry window']), v('delta', 'OFF', ['flag off'])))).toContain('Dhan ARMED BUT BLOCKED (outside the entry window)');
  });
  it('never calls a blocked attempt a sent order, and never says "off" while claiming sent orders without saying when', () => {
    const line = todayLine(v('delta', 'OFF', ['flag off'], { sent_to_broker: 1, rejected_by_broker: 1, blocked_attempts: 2 }));
    expect(line).toBe('Today: 1 sent to Delta (1 rejected by the broker) · 2 blocked attempts (never sent)');
    expect(todayLine(v('dhan', 'LIVE', [], { sent_to_broker: 0, rejected_by_broker: 0, blocked_attempts: 1 }))).toBe('Today: 0 sent to Dhan · 1 blocked attempt (never sent)');
  });
});
