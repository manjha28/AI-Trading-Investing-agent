import { describe, expect, it } from 'vitest';
import { accountLine, testModeLine, type VenueState } from './executionState';

const venue = (state: 'OFF' | 'ARMED_BUT_BLOCKED' | 'LIVE', reasons: string[] = [], liveTests = 0): VenueState => ({
  venue: 'delta', label: 'Delta', deployment_flag: { name: 'DELTA_LIVE_EXECUTION_ENABLED', enabled: true }, armed: state !== 'OFF', armed_at: null, window: { kind: 'always', text: '24x7', open_now: true },
  strategies: [], live_tests: liveTests, effective: { state, reasons }, limits: {}, today: { sent_to_broker: 0, rejected_by_broker: 0, blocked_attempts: 0 },
});

describe('forward test mode lines (TEST DATA states)', () => {
  it('says LIVE only when the account is LIVE and the test is flagged live', () => {
    expect(testModeLine(venue('LIVE', [], 1), true)).toBe('LIVE: orders are sent to Delta');
    expect(testModeLine(venue('LIVE', [], 1), false)).toBe('Paper: no orders sent');
  });
  it('a live-flagged test on a blocked or off account is paper, with the reason, never a contradiction', () => {
    expect(testModeLine(venue('ARMED_BUT_BLOCKED', ['stale price']), true)).toContain('Paper: no orders sent');
    expect(testModeLine(venue('ARMED_BUT_BLOCKED', ['stale price']), true)).not.toContain('LIVE: orders');
    expect(testModeLine(venue('OFF', ['not armed']), true)).toContain('Paper: no orders sent');
    expect(testModeLine(null, true)).toBe('Paper: no orders sent');
  });
  it('one account line per state', () => {
    expect(accountLine(venue('LIVE', [], 1))).toBe('Account: LIVE · Delta orders can be sent now (1 test flagged live)');
    expect(accountLine(venue('OFF', ['you have not armed live execution']))).toBe('Account: OFF · no orders are sent to Delta (you have not armed live execution)');
    expect(accountLine(venue('ARMED_BUT_BLOCKED', ['x']))).toContain('ARMED but blocked');
  });
});
